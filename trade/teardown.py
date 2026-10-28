"""Remove everything deploy.py created. Leaves Route53 zone, and never touches any CDKToolkit stack.

    python teardown.py

The Schwab token bucket, CMK and publisher user belong to the token publisher and are NOT touched.

Does NOT cancel Schwab orders. Cancel open app orders from the portal (or Schwab) first.
"""
import sys, json, time, pathlib
import boto3, botocore
from config import *

STATE = pathlib.Path(__file__).with_name(".deploy-state.json")
st = json.loads(STATE.read_text()) if STATE.exists() else {}

def quiet(fn, *a, **k):
    try:
        return fn(*a, **k)
    except botocore.exceptions.ClientError as e:
        print("   (skip)", e.response["Error"]["Code"], e.response["Error"].get("Message", "")[:120])

r53 = boto3.client("route53")
apigw = boto3.client("apigatewayv2", region_name=REGION)
acm = boto3.client("acm", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
ev = boto3.client("events", region_name=REGION)
iam = boto3.client("iam")
ddb = boto3.client("dynamodb", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)

if DOMAIN:
    print("DNS record")
    for rr in r53.list_resource_record_sets(HostedZoneId=HOSTED_ZONE_ID, StartRecordName=DOMAIN, MaxItems="5")["ResourceRecordSets"]:
        if rr["Name"].rstrip(".") == DOMAIN and rr["Type"] == "A":
            quiet(r53.change_resource_record_sets, HostedZoneId=HOSTED_ZONE_ID,
                  ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": rr}]})

    print("API custom domain")
    quiet(apigw.delete_domain_name, DomainName=DOMAIN)

    print("ACM cert + validation record")
    for c in acm.list_certificates()["CertificateSummaryList"]:
        if c["DomainName"] == DOMAIN:
            d = acm.describe_certificate(CertificateArn=c["CertificateArn"])["Certificate"]
            for o in d.get("DomainValidationOptions", []):
                rec = o.get("ResourceRecord")
                if rec:
                    quiet(r53.change_resource_record_sets, HostedZoneId=HOSTED_ZONE_ID, ChangeBatch={"Changes": [{
                        "Action": "DELETE", "ResourceRecordSet": {"Name": rec["Name"], "Type": rec["Type"], "TTL": 300,
                                                                 "ResourceRecords": [{"Value": rec["Value"]}]}}]})
            for _ in range(10):  # domain deletion releases the cert asynchronously
                try:
                    acm.delete_certificate(CertificateArn=c["CertificateArn"]); break
                except botocore.exceptions.ClientError as e:
                    if e.response["Error"]["Code"] != "ResourceInUseException":
                        print("   (skip)", e); break
                    time.sleep(15)

print("HTTP API")
for a in apigw.get_apis()["Items"]:
    if a["Name"] == HTTP_API:
        quiet(apigw.delete_api, ApiId=a["ApiId"])

print("schedule")
quiet(ev.remove_targets, Rule=RULE, Ids=["engine"])
quiet(ev.delete_rule, Name=RULE)

print("lambdas")
for fn in (ENGINE_FN, API_FN):
    quiet(lam.delete_function, FunctionName=fn)
    quiet(logs.delete_log_group, logGroupName=f"/aws/lambda/{fn}")

print("role")
pol =quiet(iam.list_role_policies, RoleName=ROLE)
for name in (pol or {}).get("PolicyNames", []):
    quiet(iam.delete_role_policy, RoleName=ROLE, PolicyName=name)
quiet(iam.delete_role, RoleName=ROLE)

print("dynamodb")
quiet(ddb.delete_table, TableName=TABLE)

STATE.unlink(missing_ok=True)
print("done")
