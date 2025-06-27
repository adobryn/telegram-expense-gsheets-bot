import os
import logging
import base64
import json
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# States for conversation
CATEGORY, AMOUNT_AND_DESCRIPTION = range(2)

# Constants
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

category_map = {}  # category name -> column index
current_month_cache = None  # Cache for current month to avoid repeated calls

# Create main keyboard that will be shown for quick access
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["➕ Add Expense", "📊 Categories"],
            ["📝 Open Spreadsheet", "ℹ️ Help"]
        ],
        resize_keyboard=True
    )

# Google Sheets Setup
def get_sheets_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    b64_data = os.environ["GOOGLE_CREDS_JSON"]
    json_str = base64.b64decode(b64_data).decode("utf-8")
    creds_dict = json.loads(json_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    return gspread.authorize(creds)

def get_current_month():
    """Get current month and cache it to avoid repeated datetime calls"""
    global current_month_cache
    current_month = datetime.now().strftime("%B %Y")
    
    # If month changed, clear category cache to force reload
    if current_month_cache != current_month:
        global category_map
        category_map = {}
        current_month_cache = current_month
        logging.info(f"Month changed to {current_month}, cleared category cache")
    
    return current_month

def setup_monthly_worksheet():
    global category_map
    current_month_tab = get_current_month()  # Get current month dynamically
    client = get_sheets_client()
    sheet = client.open_by_key(SPREADSHEET_ID)

    try:
        # Try to get the current month worksheet
        worksheet = sheet.worksheet(current_month_tab)
        logging.info(f"Using existing worksheet for {current_month_tab}")
    except gspread.exceptions.WorksheetNotFound:
        # If it doesn't exist, create it and copy the headers from the previous month
        logging.info(f"Creating new worksheet for {current_month_tab}")
        
        # Create the new worksheet first
        worksheet = sheet.add_worksheet(title=current_month_tab, rows=1000, cols=40)
        
        # Get all worksheets and find the most recent previous month
        all_worksheets = sheet.worksheets()
        previous_worksheet = None
        
        # Look for worksheets with month-year format and find the most recent one
        for ws in all_worksheets:
            if ws.title != current_month_tab and ws.title != "Sheet1":  # Skip default sheet
                try:
                    # Try to parse the worksheet title as a date to find previous months
                    datetime.strptime(ws.title, "%B %Y")
                    previous_worksheet = ws
                    break  # Use the first valid month worksheet found
                except ValueError:
                    continue  # Skip non-date worksheets
        
        # If we found a previous month worksheet, copy its header row
        if previous_worksheet:
            logging.info(f"Found previous worksheet: {previous_worksheet.title}")
            
            # Get the first row (header row) from the previous worksheet
            header_row = previous_worksheet.row_values(1)
            
            # If the header has content, copy it to our new worksheet
            if header_row and any(cell.strip() for cell in header_row if cell):
                # Update the entire first row at once
                worksheet.update('A1', [header_row])
                logging.info(f"Copied header row from {previous_worksheet.title}: {header_row}")
            else:
                logging.warning(f"No valid headers found in {previous_worksheet.title}")
        else:
            logging.warning("No previous month worksheet found to copy headers from")

    # Always rescan categories from the current worksheet
    headers = worksheet.row_values(1)
    
    # Clear and rebuild category map
    category_map = {}
    for col_index, value in enumerate(headers, start=1):
        if value and value.strip():  # Check for both None and empty strings
            category_map[value.strip()] = col_index
    
    logging.info(f"Rescanned categories for {current_month_tab}: Found {len(category_map)} categories: {list(category_map.keys())}")
    
    return worksheet


def add_expense_to_sheet(category, amount_description):
    worksheet = setup_monthly_worksheet()

    if category not in category_map:
        raise ValueError(f"Category '{category}' not found in spreadsheet.")

    category_col = category_map[category]
    description_col = category_col + 1  # Assuming description is always in next column
    
    # Get all values in the category column to find first empty cell
    col_values = worksheet.col_values(category_col)

    # Find the first empty row
    start_row = 2
    for i, value in enumerate(col_values[1:], start=2):
        if value.strip():
            start_row = i + 1
        else:
            break

    # Split amount and description
    amount, *description_parts = amount_description.split(" ", 1)
    description = description_parts[0] if description_parts else ""
    
    logging.info(f"Adding expense: {amount} to {category} (col {category_col}) at row {start_row}")
    
    # Update the cells
    worksheet.update_cell(start_row, category_col, amount)
    worksheet.update_cell(start_row, description_col, description)


# Bot commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Start command received!")
    if not category_map:
        setup_monthly_worksheet()

    await update.message.reply_text(
        "Welcome to the Expense Tracker Bot!\n\n"
        "Use the keyboard below for quick access to commands or type:\n"
        "/expense - Add a new expense\n"
        "/categories - See available categories\n"
        "/spreadsheet - Open your expense spreadsheet\n"
        "/help - Get help with using the bot",
        reply_markup=get_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Expense Tracker Help*\n\n"
        "*Available Commands:*\n"
        "/expense - Add a new expense\n"
        "/categories - View available expense categories\n"
        "/spreadsheet - Open your expense spreadsheet\n"
        "/help - Show this help message\n\n"
        "*How to Add an Expense:*\n"
        "1. Press 'Add Expense' or use /expense\n"
        "2. Select a category\n"
        "3. Enter amount and description\n\n"
        "*Example:* 25.50 Groceries at Walmart",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "add_expense":
        return await expense_start(update, context)


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Categories command received!")
    if not category_map:
        setup_monthly_worksheet()
    categories_list = "\n".join([f"• {category}" for category in category_map])
    await update.message.reply_text(
        f"Available expense categories:\n\n{categories_list}",
        reply_markup=get_main_keyboard()
    )

async def open_spreadsheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for opening the spreadsheet on the current month tab"""
    current_month_tab = get_current_month()  # Get current month dynamically
    client = get_sheets_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    
    # Ensure the current month worksheet exists
    try:
        worksheet = sheet.worksheet(current_month_tab)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=current_month_tab, rows=1000, cols=40)
    
    # Get the worksheet ID for the current month
    worksheet_id = worksheet.id
    
    # Create URL that opens directly to the current month tab
    spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={worksheet_id}"
    
    # Create an inline keyboard with the link
    keyboard = [[InlineKeyboardButton("Open Current Month", url=spreadsheet_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"📝 *Your Expense Spreadsheet - {current_month_tab}*\n\n"
        "Click the button below to open your current month's expense sheet:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Expense command received!")
    if not category_map:
        setup_monthly_worksheet()
    
    if not category_map:
        # No categories found
        if update.message:
            await update.message.reply_text(
                "❌ No categories found in your spreadsheet. Please add category headers in row 1.", 
                reply_markup=get_main_keyboard()
            )
        elif update.callback_query:
            await update.callback_query.message.reply_text(
                "❌ No categories found in your spreadsheet. Please add category headers in row 1.",
                reply_markup=get_main_keyboard()
            )
        return ConversationHandler.END

    # Create a keyboard with 2 categories per row
    keyboard = []
    row = []
    
    # Sort categories alphabetically for better user experience
    sorted_categories = sorted(category_map.keys())
    
    for i, category in enumerate(sorted_categories):
        row.append(InlineKeyboardButton(category, callback_data=f"cat_{category}"))
        if len(row) == 2 or i == len(sorted_categories) - 1:
            keyboard.append(row)
            row = []
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Determine how to respond (via /expense or button press)
    if update.message:
        await update.message.reply_text("Please select the expense category:", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text("Please select the expense category:", reply_markup=reply_markup)

    return CATEGORY


async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    category = query.data.replace("cat_", "")
    context.user_data["category"] = category

    # Create keyboard with change category and cancel options
    keyboard = [
        [InlineKeyboardButton("🔄 Change Category", callback_data="change_category")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"✅ Category selected: *{category}*\n\n"
        "Please enter the amount and description in one message.\n"
        "Format: [amount] [description]\n"
        "Example: 25.10 street food with family\n\n"
        "Or use the buttons below:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return AMOUNT_AND_DESCRIPTION

async def change_category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for change category button"""
    query = update.callback_query
    await query.answer()
    
    # Clear the stored category
    if "category" in context.user_data:
        del context.user_data["category"]
    
    # Show category selection again
    if not category_map:
        setup_monthly_worksheet()
    
    # Create a keyboard with 2 categories per row
    keyboard = []
    row = []
    
    # Sort categories alphabetically for better user experience
    sorted_categories = sorted(category_map.keys())
    
    for i, category in enumerate(sorted_categories):
        row.append(InlineKeyboardButton(category, callback_data=f"cat_{category}"))
        if len(row) == 2 or i == len(sorted_categories) - 1:
            keyboard.append(row)
            row = []
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text("Please select the expense category:", reply_markup=reply_markup)
    return CATEGORY

async def cancel_expense_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for cancel button"""
    query = update.callback_query
    await query.answer()
    
    # Clear user data
    context.user_data.clear()
    
    await query.edit_message_text(
        "❌ Expense entry cancelled.\n\n"
        "What would you like to do next?"
    )
    
    # Send new message with main keyboard
    await query.message.reply_text(
        "Use the keyboard below for quick access:",
        reply_markup=get_main_keyboard()
    )
    
    return ConversationHandler.END

async def amount_and_description_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parts = text.split(' ', 1)

    try:
        amount_str = parts[0].replace(',', '.')

        description = parts[1] if len(parts) > 1 else ""
        amount_description = f"{amount_str} {description}".strip()

        category = context.user_data["category"]

        try:
            add_expense_to_sheet(category, amount_description)
            await update.message.reply_text(
                f"✅ Expense added successfully!\n\n"
                f"Category: {category}\n"
                f"Entry: {amount_description}\n\n"
                "Use the keyboard below to continue.",
                reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logging.error(f"Error adding expense to sheets: {e}")
            await update.message.reply_text(
                f"❌ Error saving expense: {str(e)}\n"
                "Please try again.",
                reply_markup=get_main_keyboard()
            )

    except (ValueError, IndexError):
        await update.message.reply_text(
            "Please enter a valid amount and description.\n"
            "Format: [amount] [description]\n"
            "Example: 25.10 street food with family"
        )
        return AMOUNT_AND_DESCRIPTION

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "Operation cancelled. What would you like to do next?",
            reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "Operation cancelled. What would you like to do next?",
            reply_markup=get_main_keyboard()
        )
    return ConversationHandler.END

# Handle text messages that match our keyboard buttons
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == "➕ Add Expense":
        return await expense_start(update, context)
    elif text == "📊 Categories":
        await show_categories(update, context)
    elif text == "📝 Open Spreadsheet":
        await open_spreadsheet(update, context)
    elif text == "ℹ️ Help":
        await help_command(update, context)
    else:
        await update.message.reply_text(
            "I don't understand that command. Please use the keyboard or type / to see available commands.",
            reply_markup=get_main_keyboard()
        )


def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Create a post-init hook to set up commands
    async def post_init(application):
        await application.bot.set_my_commands([
            ("expense", "Add a new expense"),
            ("categories", "View available categories"), 
            ("spreadsheet", "Open your expense spreadsheet"),
            ("help", "Get help with using the bot")
        ])
    
    # Add the post-init hook to the application
    application.post_init = post_init

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("categories", show_categories))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("spreadsheet", open_spreadsheet))

    # Add conversation handler for adding expenses
    conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("expense", expense_start),
        CallbackQueryHandler(handle_buttons, pattern="^add_expense$"),
        MessageHandler(filters.Text(["➕ Add Expense"]), expense_start)
    ],
    states={
        CATEGORY: [
            CallbackQueryHandler(category_selected, pattern=r"^cat_")
        ],
        AMOUNT_AND_DESCRIPTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, amount_and_description_entered),
            CallbackQueryHandler(change_category_handler, pattern="^change_category$"),
            CallbackQueryHandler(cancel_expense_handler, pattern="^cancel_expense$")
        ]
    },
    fallbacks=[],
    )
    application.add_handler(conv_handler)
    
    # Handler for other text messages (keyboard buttons)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Text(["➕ Add Expense"]), 
        handle_text_messages
    ))

    print("Bot is starting...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"Error starting bot: {e}")
        logging.error(f"Error starting bot: {e}")

if __name__ == "__main__":
    main()