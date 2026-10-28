"""Switch order pricing.

    python set_mode.py demo     # limit = 10% under the recommended price (default at deploy)
    python set_mode.py live     # limit = the recommended (current) price
    python set_mode.py          # show current mode
"""
import sys
import boto3
from config import REGION, TABLE, DEMO_DISCOUNT

t = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
if len(sys.argv) > 1:
    if sys.argv[1] not in ("demo", "live"):
        sys.exit(__doc__)
    t.update_item(Key={"pk": "CONFIG", "sk": "main"}, UpdateExpression="SET demo_mode = :d, demo_discount = :x",
                  ExpressionAttributeValues={":d": sys.argv[1] == "demo", ":x": __import__("decimal").Decimal(str(DEMO_DISCOUNT))})
print(t.get_item(Key={"pk": "CONFIG", "sk": "main"}, ConsistentRead=True)["Item"])
