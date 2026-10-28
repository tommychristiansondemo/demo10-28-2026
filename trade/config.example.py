"""Names and settings shared by deploy.py / teardown.py / set_mode.py.

Copy to config.py (git-ignored) and fill in your own values:   cp config.example.py config.py
"""
REGION = "us-west-2"
ACCOUNT_ID = "123456789012"          # your AWS account id
PREFIX = "trade-demo"
TAGS = {"project": "trade-demo"}

# Optional custom domain in a Route 53 hosted zone you own. Leave DOMAIN = "" to use the
# API Gateway default URL (printed by deploy.py as api_endpoint).
DOMAIN = ""                          # e.g. "trade.example.com"
HOSTED_ZONE_ID = ""                  # e.g. "Z0123456789ABCDEFGHIJ"

# Token side: written by YOUR token publisher (see projectknowledge.md). The app only reads it.
TOKEN_BUCKET = f"schwab-demo-tokens-{ACCOUNT_ID}"
TOKEN_KEY = "schwab/access.json"
KMS_ALIAS = "alias/schwab-demo"

TABLE = PREFIX
ROLE = f"{PREFIX}-lambda-role"
ENGINE_FN = f"{PREFIX}-engine"
API_FN = f"{PREFIX}-api"
RULE = f"{PREFIX}-every-minute"
HTTP_API = f"{PREFIX}-http"

MODEL_ID = "us.anthropic.claude-sonnet-4-6"   # any Bedrock inference profile your account can invoke
SCHWAB_ACCOUNT_NUMBER = "00000000"   # plain account number; only ever lives in Lambda env, never sent to the browser
EXPIRATION = "2026-11-06"            # option expiration the spreads are built for (a Friday)
MAX_ORDER_USD = 500                  # per-order cap
MAX_TOTAL_USD = 1500                 # bankroll; the last order shrinks to fit what's left
DISPLAY_OFFSET = 0                   # optional: students see MAX_TOTAL_USD + this; display only
MAX_ACTIVE_ORDERS = 3
MAX_ORDERS_PER_DAY = 20
DEMO_DISCOUNT = 0.10                 # demo mode: limit price this far under the recommendation
