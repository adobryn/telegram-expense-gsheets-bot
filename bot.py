"""Telegram expense tracker bot backed by Google Sheets.

Refactored version:
- All gspread I/O is encapsulated in SheetService and executed via
  asyncio.to_thread so the event loop is never blocked.
- Worksheet + category map are cached per month; the cache invalidates
  automatically when the month rolls over.
- Amount is validated as a number and written with USER_ENTERED so the
  sheet treats it as a numeric value (SUM etc. keep working).
- Both cells (amount + description) are written in one batched update
  instead of two API calls.
- Category buttons use stable indices as callback_data (Telegram limits
  callback_data to 64 bytes; long category names used to break this).
"""

import asyncio
import base64
import json
import logging
import os
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
CATEGORY, AMOUNT_AND_DESCRIPTION = range(2)

# Reply-keyboard button labels
BTN_ADD_EXPENSE = "➕ Add Expense"
BTN_CATEGORIES = "📊 Categories"
BTN_SPREADSHEET = "📝 Open Spreadsheet"
BTN_HELP = "ℹ️ Help"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_ADD_EXPENSE, BTN_CATEGORIES],
        [BTN_SPREADSHEET, BTN_HELP],
    ],
    resize_keyboard=True,
)


# ---------------------------------------------------------------------------
# Google Sheets service
# ---------------------------------------------------------------------------
class SheetService:
    """Owns the gspread client and the per-month worksheet/category cache."""

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self, spreadsheet_id: str, creds_b64: str):
        self._spreadsheet_id = spreadsheet_id
        self._creds_b64 = creds_b64
        self._client: gspread.Client | None = None
        self._worksheet: gspread.Worksheet | None = None
        self._month: str | None = None
        self._categories: dict[str, int] = {}

    # -- internals ---------------------------------------------------------

    def _get_client(self) -> gspread.Client:
        if self._client is None:
            info = json.loads(base64.b64decode(self._creds_b64).decode("utf-8"))
            creds = Credentials.from_service_account_info(info, scopes=self.SCOPES)
            self._client = gspread.authorize(creds)
        return self._client

    @staticmethod
    def _current_month() -> str:
        return datetime.now().strftime("%B %Y")

    @staticmethod
    def _latest_month_header(sheet: gspread.Spreadsheet, exclude_title: str):
        """Header row of the most recent previous month tab, or None."""
        candidates = []
        for ws in sheet.worksheets():
            if ws.title == exclude_title:
                continue
            try:
                parsed = datetime.strptime(ws.title, "%B %Y")
            except ValueError:
                continue  # not a month tab
            candidates.append((parsed, ws))

        if not candidates:
            return None

        _, previous = max(candidates, key=lambda item: item[0])
        header = previous.row_values(1)
        if header and any(cell.strip() for cell in header if cell):
            logger.info("Copying headers from previous tab '%s'", previous.title)
            return header
        logger.warning("Previous tab '%s' has no usable header row", previous.title)
        return None

    def _ensure_worksheet(self) -> gspread.Worksheet:
        """Return the worksheet for the current month, creating it if needed.

        Cached per month; the cache (worksheet + categories) is dropped
        automatically when the month changes.
        """
        month = self._current_month()
        if self._worksheet is not None and self._month == month:
            return self._worksheet

        logger.info("(Re)loading worksheet for %s", month)
        sheet = self._get_client().open_by_key(self._spreadsheet_id)
        try:
            worksheet = sheet.worksheet(month)
        except gspread.exceptions.WorksheetNotFound:
            logger.info("Creating new worksheet '%s'", month)
            worksheet = sheet.add_worksheet(title=month, rows=1000, cols=40)
            header = self._latest_month_header(sheet, exclude_title=month)
            if header:
                worksheet.update(range_name="A1", values=[header])

        self._worksheet = worksheet
        self._month = month
        self._categories = {}  # force a rescan for the new tab
        return worksheet

    def _refresh_categories(self) -> dict[str, int]:
        worksheet = self._ensure_worksheet()
        headers = worksheet.row_values(1)
        self._categories = {
            value.strip(): col
            for col, value in enumerate(headers, start=1)
            if value and value.strip()
        }
        logger.info(
            "Categories for %s: %s", self._month, list(self._categories)
        )
        return dict(self._categories)

    # -- public API (sync; call via asyncio.to_thread) ----------------------

    def get_categories(self) -> dict[str, int]:
        """Category name -> column index for the current month tab."""
        self._ensure_worksheet()
        if not self._categories:
            self._refresh_categories()
        return dict(self._categories)

    def add_expense(self, category: str, amount: float, description: str) -> int:
        """Append an expense; returns the row it was written to."""
        categories = self.get_categories()
        if category not in categories:
            # Header row may have changed since the buttons were rendered.
            categories = self._refresh_categories()
        if category not in categories:
            raise ValueError(f"Category '{category}' not found in spreadsheet.")

        worksheet = self._worksheet
        col = categories[category]

        # col_values() never returns trailing empty cells, so the first free
        # row is simply len + 1 (row 1 is the header).
        row = max(len(worksheet.col_values(col)) + 1, 2)

        cell_range = f"{rowcol_to_a1(row, col)}:{rowcol_to_a1(row, col + 1)}"
        worksheet.update(
            range_name=cell_range,
            values=[[amount, description]],
            value_input_option="USER_ENTERED",
        )
        logger.info(
            "Added %.2f to '%s' (col %d, row %d)", amount, category, col, row
        )
        return row

    def get_current_month_url(self) -> str:
        worksheet = self._ensure_worksheet()
        return (
            f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}"
            f"/edit#gid={worksheet.id}"
        )

    @property
    def current_month(self) -> str:
        return self._current_month()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_expense_input(text: str) -> tuple[float, str]:
    """Parse '25,10 street food' into (25.10, 'street food').

    Raises ValueError if the first token is not a positive number.
    """
    parts = text.strip().split(" ", 1)
    amount = float(parts[0].replace(",", "."))
    if amount <= 0:
        raise ValueError("Amount must be positive.")
    description = parts[1].strip() if len(parts) > 1 else ""
    return amount, description


def build_category_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    """Two buttons per row; callback_data carries the index, not the name
    (callback_data is limited to 64 bytes)."""
    keyboard = []
    row = []
    for index, category in enumerate(categories):
        row.append(InlineKeyboardButton(category, callback_data=f"cat_{index}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")])
    return InlineKeyboardMarkup(keyboard)


def sheets(context: ContextTypes.DEFAULT_TYPE) -> SheetService:
    return context.bot_data["sheets"]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to the Expense Tracker Bot!\n\n"
        "Use the keyboard below for quick access to commands or type:\n"
        "/expense - Add a new expense\n"
        "/categories - See available categories\n"
        "/spreadsheet - Open your expense spreadsheet\n"
        "/help - Get help with using the bot",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Expense Tracker Help</b>\n\n"
        "<b>Available Commands:</b>\n"
        "/expense - Add a new expense\n"
        "/categories - View available expense categories\n"
        "/spreadsheet - Open your expense spreadsheet\n"
        "/cancel - Cancel the current operation\n"
        "/help - Show this help message\n\n"
        "<b>How to Add an Expense:</b>\n"
        "1. Press 'Add Expense' or use /expense\n"
        "2. Select a category\n"
        "3. Enter amount and description\n\n"
        "<b>Example:</b> 25.50 Groceries at Walmart",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    categories = await asyncio.to_thread(sheets(context).get_categories)
    if not categories:
        await update.message.reply_text(
            "❌ No categories found in your spreadsheet. "
            "Please add category headers in row 1.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    categories_list = "\n".join(f"• {category}" for category in sorted(categories))
    await update.message.reply_text(
        f"Available expense categories:\n\n{categories_list}",
        reply_markup=MAIN_KEYBOARD,
    )


async def open_spreadsheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = sheets(context)
    url = await asyncio.to_thread(service.get_current_month_url)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Current Month", url=url)]]
    )
    await update.message.reply_text(
        f"📝 Your Expense Spreadsheet - {service.current_month}\n\n"
        "Click the button below to open your current month's expense sheet:",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Expense conversation
# ---------------------------------------------------------------------------
async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    categories = await asyncio.to_thread(sheets(context).get_categories)
    message = update.message or update.callback_query.message

    if not categories:
        await message.reply_text(
            "❌ No categories found in your spreadsheet. "
            "Please add category headers in row 1.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    sorted_categories = sorted(categories)
    context.user_data["categories"] = sorted_categories

    await message.reply_text(
        "Please select the expense category:",
        reply_markup=build_category_keyboard(sorted_categories),
    )
    return CATEGORY


async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    categories = context.user_data.get("categories", [])
    try:
        category = categories[int(query.data.removeprefix("cat_"))]
    except (ValueError, IndexError):
        # Stale buttons (e.g. bot restarted since they were rendered).
        await query.edit_message_text(
            "⚠️ That menu is out of date. Please start again with /expense."
        )
        return ConversationHandler.END

    context.user_data["category"] = category

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Change Category", callback_data="change_category")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")],
        ]
    )
    await query.edit_message_text(
        f"✅ Category selected: {category}\n\n"
        "Please enter the amount and description in one message.\n"
        "Format: [amount] [description]\n"
        "Example: 25.10 street food with family\n\n"
        "Or use the buttons below:",
        reply_markup=keyboard,
    )
    return AMOUNT_AND_DESCRIPTION


async def change_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("category", None)

    categories = await asyncio.to_thread(sheets(context).get_categories)
    sorted_categories = sorted(categories)
    context.user_data["categories"] = sorted_categories

    await query.edit_message_text(
        "Please select the expense category:",
        reply_markup=build_category_keyboard(sorted_categories),
    )
    return CATEGORY


async def cancel_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()

    await query.edit_message_text("❌ Expense entry cancelled.")
    await query.message.reply_text(
        "What would you like to do next?",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def amount_and_description_entered(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    category = context.user_data.get("category")
    if category is None:
        await update.message.reply_text(
            "⚠️ I lost track of the selected category. "
            "Please start again with /expense.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    try:
        amount, description = parse_expense_input(update.message.text)
    except ValueError:
        await update.message.reply_text(
            "Please enter a valid amount and description.\n"
            "Format: [amount] [description]\n"
            "Example: 25.10 street food with family"
        )
        return AMOUNT_AND_DESCRIPTION

    try:
        await asyncio.to_thread(
            sheets(context).add_expense, category, amount, description
        )
    except Exception:
        logger.exception("Error adding expense to sheet")
        await update.message.reply_text(
            "❌ Error saving the expense. Please try again.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    entry = f"{amount:g} {description}".strip()
    await update.message.reply_text(
        "✅ Expense added successfully!\n\n"
        f"Category: {category}\n"
        f"Entry: {entry}\n\n"
        "Use the keyboard below to continue.",
        reply_markup=MAIN_KEYBOARD,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Operation cancelled. What would you like to do next?",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Fallback text handler + error handler
# ---------------------------------------------------------------------------
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == BTN_CATEGORIES:
        await show_categories(update, context)
    elif text == BTN_SPREADSHEET:
        await open_spreadsheet(update, context)
    elif text == BTN_HELP:
        await help_command(update, context)
    else:
        await update.message.reply_text(
            "I don't understand that command. Please use the keyboard "
            "or type / to see available commands.",
            reply_markup=MAIN_KEYBOARD,
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "❌ Something went wrong. Please try again.",
            reply_markup=MAIN_KEYBOARD,
        )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
async def post_init(application):
    await application.bot.set_my_commands(
        [
            ("expense", "Add a new expense"),
            ("categories", "View available categories"),
            ("spreadsheet", "Open your expense spreadsheet"),
            ("cancel", "Cancel the current operation"),
            ("help", "Get help with using the bot"),
        ]
    )


def main():
    bot_token = os.environ["BOT_TOKEN"]
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    creds_b64 = os.environ["GOOGLE_CREDS_JSON"]

    application = (
        ApplicationBuilder().token(bot_token).post_init(post_init).build()
    )
    application.bot_data["sheets"] = SheetService(spreadsheet_id, creds_b64)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("categories", show_categories))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("spreadsheet", open_spreadsheet))

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("expense", expense_start),
            MessageHandler(filters.Text([BTN_ADD_EXPENSE]), expense_start),
        ],
        states={
            CATEGORY: [
                CallbackQueryHandler(category_selected, pattern=r"^cat_\d+$"),
                CallbackQueryHandler(cancel_expense, pattern="^cancel_expense$"),
            ],
            AMOUNT_AND_DESCRIPTION: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    amount_and_description_entered,
                ),
                CallbackQueryHandler(change_category, pattern="^change_category$"),
                CallbackQueryHandler(cancel_expense, pattern="^cancel_expense$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conv_handler)

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages)
    )
    application.add_error_handler(on_error)

    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
