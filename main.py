import os
import time
import json
import logging
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import telebot
from telebot.types import Message
from quiz_handler import parse_quiz_text, Question

# --- Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Load env
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID') or 0)
QUIZ_GROUP_ID = int(os.getenv('QUIZ_GROUP_ID') or 0)
STORAGE_GROUP_ID = int(os.getenv('STORAGE_GROUP_ID') or 0)
DEFAULT_NEGATIVE = float(os.getenv('DEFAULT_NEGATIVE') or 1/3)
DEFAULT_TIMER = int(os.getenv('DEFAULT_TIMER') or 30)

if not BOT_TOKEN or not ADMIN_ID or not QUIZ_GROUP_ID or not STORAGE_GROUP_ID:
    logger.error('Missing required .env settings. Please fill BOT_TOKEN, ADMIN_ID, QUIZ_GROUP_ID and STORAGE_GROUP_ID')
    raise SystemExit('Incomplete .env')

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# In-memory state for running/scheduled quizzes
SCHEDULED_QUIZZES = {}  # job_id -> {"questions": [...], "original_msg": Message, ...}
ACTIVE_POLLS = {}  # poll_id -> {chat_id, message_id, question_index, correct_option, participants_answers}

DATA_DIR = 'data'
os.makedirs(DATA_DIR, exist_ok=True)
SCORES_FILE = os.path.join(DATA_DIR, 'scores.json')

# ensure scores file
if not os.path.exists(SCORES_FILE):
    with open(SCORES_FILE, 'w') as f:
        json.dump({}, f)

scheduler = BackgroundScheduler()
scheduler.start()

# --- Utilities for scores

def load_scores():
    try:
        with open(SCORES_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_scores(scores):
    with open(SCORES_FILE, 'w') as f:
        json.dump(scores, f, indent=2)


# --- Admin-only decorator
def admin_only(func):
    def wrapper(message: Message):
        from telebot import types
        user_id = message.from_user.id if message.from_user else None
        if user_id != ADMIN_ID:
            bot.reply_to(message, "❌ You are not authorized to use this bot.")
            return
        return func(message)
    return wrapper


# --- Command: /start (admin only)
@bot.message_handler(commands=['start'])
@admin_only
def start_cmd(message: Message):
    bot.reply_to(message, "Strivane Quiz Bot is online. Send a .txt file or paste quiz text to schedule. Use /schedule YYYY-MM-DD HH:MM to schedule the last uploaded quiz.")


# Hold the last uploaded quiz per admin session
LAST_QUIZ_STORAGE = {
    'message': None,
    'text': None,
    'file_id': None,
}


# --- Receive .txt file upload (admin only)
@bot.message_handler(content_types=['document'])
@admin_only
def handle_document(message: Message):
    doc = message.document
    if not doc.file_name.lower().endswith('.txt'):
        bot.reply_to(message, 'Please upload a .txt file containing the quiz in the proper format.')
        return
    file_info = bot.get_file(doc.file_id)
    file_bytes = bot.download_file(file_info.file_path)
    text = file_bytes.decode('utf-8')
    LAST_QUIZ_STORAGE['message'] = message
    LAST_QUIZ_STORAGE['text'] = text
    LAST_QUIZ_STORAGE['file_id'] = doc.file_id
    bot.reply_to(message, f'✅ Quiz file received. Use /schedule YYYY-MM-DD HH:MM to schedule it in the group {QUIZ_GROUP_ID}.')


# --- Receive pasted text (admin only)
@bot.message_handler(func=lambda m: m.content_type == 'text')
@admin_only
def handle_text(message: Message):
    text = message.text.strip()
    # If this is a schedule command, handle below
    if text.lower().startswith('/schedule'):
        # pass to schedule handler
        schedule_command(message)
        return

    # Otherwise treat as quiz text
    LAST_QUIZ_STORAGE['message'] = message
    LAST_QUIZ_STORAGE['text'] = text
    LAST_QUIZ_STORAGE['file_id'] = None
    bot.reply_to(message, '✅ Quiz text received. Use /schedule YYYY-MM-DD HH:MM to schedule it.')


# --- Schedule command (admin only)
@admin_only
def schedule_command(message: Message):
    text = message.text.strip()
    parts = text.split()
    if len(parts) < 3:
        bot.reply_to(message, 'Usage: /schedule YYYY-MM-DD HH:MM')
        return
    try:
        date = parts[1]
        timepart = parts[2]
        from datetime import datetime
        dt = datetime.strptime(f"{date} {timepart}", "%Y-%m-%d %H:%M")
    except Exception as e:
        bot.reply_to(message, 'Invalid datetime format. Use /schedule YYYY-MM-DD HH:MM')
        return

    if not LAST_QUIZ_STORAGE['text']:
        bot.reply_to(message, 'No quiz uploaded yet. Please upload a .txt file or paste quiz text first.')
        return

    # parse quiz
    questions = parse_quiz_text(LAST_QUIZ_STORAGE['text'], default_negative=DEFAULT_NEGATIVE, default_time=DEFAULT_TIMER)
    if not questions:
        bot.reply_to(message, 'Failed to parse quiz. Please check format.')
        return

    job_id = f"quiz_{int(time.time())}"

    def job_func(qs=questions, orig_msg=LAST_QUIZ_STORAGE['message'], jid=job_id):
        try:
            run_quiz_job(jid, qs, orig_msg)
        except Exception as ex:
            logger.exception('Error running scheduled quiz: %s', ex)

    scheduler.add_job(job_func, 'date', run_date=dt, id=job_id)
    SCHEDULED_QUIZZES[job_id] = {'questions': questions, 'original_msg': LAST_QUIZ_STORAGE['message']}
    bot.reply_to(message, f'✅ Quiz scheduled for {dt.strftime("%Y-%m-%d %H:%M")}. Job id: {job_id}')


# Attach the schedule command to the /schedule handler so admin can use it
@bot.message_handler(commands=['schedule'])
@admin_only
def schedule_cmd_entry(message: Message):
    schedule_command(message)


# --- Core: run the quiz job (posts polls one by one)
def run_quiz_job(job_id: str, questions: list, orig_msg: Message):
    logger.info('Starting quiz job %s with %d questions', job_id, len(questions))
    posted_messages = []  # list of (chat_id, message_id)

    chat_id = QUIZ_GROUP_ID

    for idx, q in enumerate(questions):
        # send poll
        try:
            msg = bot.send_poll(chat_id, q.text, q.options, type='quiz', is_anonymous=False, correct_option_id=q.correct_option)
        except Exception as e:
            logger.exception('Failed to send poll: %s', e)
            continue

        poll = msg.poll
        poll_id = poll.id
        posted_messages.append((chat_id, msg.message_id))

        # register active poll
        ACTIVE_POLLS[poll_id] = {
            'chat_id': chat_id,
            'message_id': msg.message_id,
            'question_index': idx,
            'correct_option': q.correct_option,
            'negative': q.negative,
            'answers': {},  # user_id -> selected_option_index
        }

        # wait for the question time
        logger.info('Question %d posted (poll_id=%s). Waiting %d seconds', idx + 1, poll_id, q.time)
        time.sleep(q.time)

        # close poll
        try:
            bot.stop_poll(chat_id, msg.message_id)
        except Exception:
            logger.exception('Failed to stop poll for message %s', msg.message_id)

        # compute intermediate scoring for this question based on ACTIVE_POLLS[poll_id]['answers']
        compute_scores_for_poll(poll_id)

    # Quiz finished — delete posted messages from group
    logger.info('Quiz job %s finished. Deleting quiz messages from group.', job_id)
    for c, mid in posted_messages:
        try:
            bot.delete_message(c, mid)
        except Exception:
            logger.exception('Failed to delete message %s in chat %s', mid, c)

    # Forward the original quiz message (file or text message) to storage group
    try:
        orig = SCHEDULED_QUIZZES.get(job_id, {}).get('original_msg') or orig_msg
        if orig is not None:
            # forward the message
            bot.forward_message(STORAGE_GROUP_ID, orig.chat.id, orig.message_id)
    except Exception:
        logger.exception('Failed to forward original quiz message to storage group')

    # Cleanup
    if job_id in SCHEDULED_QUIZZES:
        del SCHEDULED_QUIZZES[job_id]

    logger.info('Quiz job %s complete and archived.', job_id)


# --- Poll answer handler to record each user's answer
@bot.poll_answer_handler(func=lambda a: True)
def handle_poll_answer(poll_answer):
    try:
        poll_id = poll_answer.poll_id
        user = poll_answer.user
        option_ids = poll_answer.option_ids
        if not option_ids:
            return
        selected = option_ids[0]
        if poll_id in ACTIVE_POLLS:
            ACTIVE_POLLS[poll_id]['answers'][user.id] = selected
            logger.debug('Recorded answer: user=%s poll=%s option=%s', user.id, poll_id, selected)
    except Exception:
        logger.exception('Error in poll_answer handler')


# --- Poll update handler (detect closed polls) to finalize scoring if needed
@bot.poll_handler(func=lambda p: True)
def handle_poll_update(poll):
    try:
        # When Telegram closes poll it sends a poll update with is_closed True
        if poll.is_closed and poll.id in ACTIVE_POLLS:
            compute_scores_for_poll(poll.id)
    except Exception:
        logger.exception('Error in poll update handler')


# --- Score computation

def compute_scores_for_poll(poll_id: str):
    info = ACTIVE_POLLS.get(poll_id)
    if not info:
        return
    correct = info['correct_option']
    negative = info.get('negative', DEFAULT_NEGATIVE)
    answers = info.get('answers', {})

    scores = load_scores()
    for user_id_str, selected_option in list(answers.items()):
        # user_id here is int (from poll_answer), convert to string for JSON
        uid = str(user_id_str)
        uid_int = int(user_id_str)
        if uid not in scores:
            scores[uid] = {
                'username': None,
                'attempted': 0,
                'correct': 0,
                'wrong': 0,
                'score': 0.0,
            }
        user_entry = scores[uid]
        user_entry['attempted'] += 1
        if selected_option == correct:
            user_entry['correct'] += 1
            user_entry['score'] += 1.0
        else:
            user_entry['wrong'] += 1
            user_entry['score'] -= negative

        # try to update username if available via get_chat
        try:
            u = bot.get_chat(uid_int)
            user_entry['username'] = u.username or f"{u.first_name or ''} {u.last_name or ''}".strip()
        except Exception:
            pass

    save_scores(scores)

    # once processed, remove poll from active polls to avoid double processing
    try:
        del ACTIVE_POLLS[poll_id]
    except KeyError:
        pass


# --- Run polling loop
if __name__ == '__main__':
    logger.info('Bot started. Listening for commands...')
    bot.infinity_polling()
