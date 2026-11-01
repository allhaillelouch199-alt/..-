import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
DB_PATH = os.getenv("DB_PATH", "bot.db")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")

LEAVE_DELAY_MINUTES = 5           # مغادرة القناة بعد 5 دقائق
CHANNEL_LIMIT_LEAVE_COUNT = 200   # عدد القنوات التي ستُغادر عند امتلاء الحساب
FAILURES_BEFORE_COMA = 3          # عدد الفشل قبل الغيبوبة
COMA_HOURS = 6                    # مدة الغيبوبة بالساعات

os.makedirs(SESSIONS_DIR, exist_ok=True)
