import logging
import os
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv
import requests
import redis
import time

redis_client = redis.Redis(host='localhost', port=6379, db=0)

# Load environment variables
load_dotenv()

# Bot token from environment variable
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Define states for conversation
NWC_SECRET, LN_ADDRESS, ZAP_AMOUNT = range(3)

# Start the conversation
async def start_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.type != 'private':
        await update.message.reply_text('Please use the /connect command in a private chat with me.')
        return ConversationHandler.END
    await update.message.reply_text('Please send your NWC URI:')
    return NWC_SECRET

# First step: collect NWC URI
async def set_nwc_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['nwc_secret'] = update.message.text
    await update.message.reply_text('NWC URI set. Now, send your Lightning address:')
    return LN_ADDRESS

# Second step: collect Lightning address
async def set_ln_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['ln_address'] = update.message.text
    await update.message.reply_text('Lightning address set. Finally, set your default Zap amount:')
    return ZAP_AMOUNT

# Third step: collect Zap amount and create user
async def set_zap_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        zap_amount = int(update.message.text)
        context.user_data['zap_amount'] = zap_amount
        await update.message.reply_text(f'Default Zap amount set to {zap_amount} sats. Configuration complete.')

        # Extract the Telegram user ID
        telegram_user_id = update.effective_user.id

        # Call create_user with collected data and Telegram user ID
        await create_user(str(telegram_user_id), context.user_data.get('ln_address'), context.user_data.get('nwc_secret'))

    except ValueError:
        await update.message.reply_text('Please enter a valid number for the Zap amount.')
    return ConversationHandler.END


async def handle_zap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message

    # Check if the message is in a group or channel and is a reply containing a lightning bolt emoji
    if (message.chat.type in ['group', 'supergroup', 'channel']) and message.reply_to_message and '⚡' in message.text:
        telegram_user_id = str(message.from_user.id)

        # Check if the sender is in the Redis store
        sender_id = redis_client.get(telegram_user_id)
        # check if the reply is from a registered user
        receiver_id = redis_client.get(str(message.reply_to_message.from_user.id))
        
        if sender_id and receiver_id:
            # Decode bytes to string
            sender_id = sender_id.decode("utf-8")
            receiver_id = receiver_id.decode("utf-8")

            zap_response = await send_zap(sender_id, receiver_id)
            if zap_response:
                # respond to both the sender and receiver
                await message.reply_text(f'Zap sent to {message.reply_to_message.from_user.first_name}!')
                await message.reply_to_message.reply_text(f'You received a Zap from {message.from_user.first_name}!')
        else:
            # User not found in Redis store
            await message.reply_text("You are not registered. Please use the /connect command to register.")
    else:
        # If not in a group or channel, or not a reply, or does not contain a lightning bolt, do nothing
        pass

async def send_zap(sender_id: str, receiver_id: str) -> bool:
    url = 'https://api.makeprisms.com/v0/payment'
    payload = {
        "senderId": sender_id,
        "receiverId": receiver_id,
        "amount": 21,
        "currency": "SAT"
    }
    response = requests.post(url, json=payload)

    if response.status_code == 200:
        payment_response = response.json()
        print('Initial Zap response:', payment_response)

        # Check if we need to poll for payment completion
        if payment_response.get('status') == 'sending':
            payment_id = payment_response.get('id')
            return await poll_for_payment_completion(payment_id)
        else:
            return payment_response.get('status') == 'paid'
    else:
        print('Error sending Zap: ', response.text)
        return False

async def poll_for_payment_completion(payment_id: str) -> bool:
    print('Polling for payment completion...')
    poll_url = f'https://api.makeprisms.com/v0/payment/{payment_id}'
    while True:
        poll_response = requests.get(poll_url)
        print('Poll response:', poll_response.json())
        if poll_response.status_code == 200:
            payment_status = poll_response.json().get('status')
            if payment_status == 'paid':
                print('Payment completed successfully.')
                return True
            elif payment_status != 'sending':
                # Handle other statuses like 'failed' or 'cancelled'
                print(f'Payment failed or cancelled with status: {payment_status}')
                return False
        else:
            print(f'Error polling payment status: {poll_response.text}')
            return False

        # Wait for some time before polling again
        time.sleep(5)  # Poll every 5 seconds, adjust as needed

async def create_user(telegram_user_id: str, lnAddress: str = None, nwc: str = None) -> None:
    # Create a new user
    url = 'https://api.makeprisms.com/v0/user'
    payload = {
        "lnAddress": lnAddress,
        "nwcConnection": {
            "nwcUrl": nwc,
            "connectorName": 'telegram-zap-bot',
            "connectorType": 'nwc.alby',
        }
    }
    response = requests.post(url, json=payload)
    if response.status_code == 201:
        user_data = response.json()
        print('User created successfully!', user_data)

        # Save username and ID to Redis
        user_id = user_data.get('id')

        if user_id and telegram_user_id:
            redis_client.set(telegram_user_id, user_id)
            print(f'Saved user {telegram_user_id} with ID {user_id} to Redis')
    else:
        print('Error creating user: ', response.text)

async def update_user(lnAddress: str = None, nwc: str = None) -> None:
    # Update an existing user
    url = 'https://api.makeprisms.com/v0/user'
    payload = {
        "lnAddress": lnAddress,
        "nwcConnection": {
            "nwcUrl": nwc,
            "connectorName": 'telegram-zap-bot',
            "connectorType": 'nwc.alby',
        }
    }
    response = requests.patch(url, json=payload)
    if response.status_code == 200:
        print('User updated successfully!', response.json())
    else:
        print('Error updating user: ', response.text)

# Cancel handler
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('Operation cancelled.')
    return ConversationHandler.END

async def error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, context.error)

# Main function to run the bot
def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for the /connect command
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('connect', start_connect)],
        states={
            NWC_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_nwc_secret)],
            LN_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_ln_address)],
            ZAP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_zap_amount)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT, handle_zap))
    application.add_error_handler(error)

    # Start the Bot
    application.run_polling()

if __name__ == "__main__":
    main()
