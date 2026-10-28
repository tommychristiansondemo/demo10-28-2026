"""Idempotent deploy of the AI Trade Desk demo to us-west-2.

    python deploy.py            # everything
    python deploy.py --code     # just re-upload Lambda code

Order: look up the token publisher's bucket/CMK -> table -> role -> lambdas ->
schedule -> HTTP API -> ACM cert -> custom domain -> DNS.
"""
import sys, io, json, time, zipfile, pathlib
import boto3, botocore
from config import *

HERE = pathlib.Path(__file__).parent
STATE = HERE / ".deploy-state.json"
st = json.loads(STATE.read_text()) if STATE.exists() else {}
def save(): STATE.write_text(json.dumps(st, indent=1))

s3 = boto3.client("s3", region_name=REGION)
kms = boto3.client("kms", region_name=REGION)
iam = boto3.client("iam")
ddb = boto3.client("dynamodb", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
ev = boto3.client("events", region_name=REGION)
apigw = boto3.client("apigatewayv2", region_name=REGION)
acm = boto3.client("acm", region_name=REGION)
r53 = boto3.client("route53")
taglist = [{"Key": k, "Value": v} for k, v in TAGS.items()]


def exists(fn, *a, **k):
    try:
        return fn(*a, **k)
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchEntity", "NotFoundException", "ResourceNotFoundException",
                                            "NoSuchBucket", "404", "NotFound"):
            return None
        raise


def token_side():
    """Look up the bucket and CMK your token publisher writes to (see projectknowledge.md). Never created here."""
    print("== token side (owned by the token publisher)")
    kid = kms.describe_key(KeyId=KMS_ALIAS)["KeyMetadata"]["KeyId"]
    s3.head_bucket(Bucket=TOKEN_BUCKET)
    st["kms_key_id"] = kid
    st["kms_key_arn"] = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/{kid}"
    save()


def table():
    print("== table", TABLE)
    if not exists(ddb.describe_table, TableName=TABLE):
        ddb.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST", Tags=taglist,
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
        ddb.get_waiter("table_exists").wait(TableName=TABLE)
        ddb.put_item(TableName=TABLE, Item={"pk": {"S": "CONFIG"}, "sk": {"S": "main"},
                                            "demo_mode": {"BOOL": True}, "demo_discount": {"N": str(DEMO_DISCOUNT)}})


def role():
    print("== role", ROLE)
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                                      "Action": "sts:AssumeRole"}]}
    r = exists(iam.get_role, RoleName=ROLE)
    if not r:
        r = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust), Tags=taglist)
        time.sleep(10)
    st["role_arn"] = r["Role"]["Arn"]
    model = MODEL_ID.split(".", 1)[1]
    iam.put_role_policy(RoleName=ROLE, PolicyName="app", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:*"},
        {"Sid": "Table", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                                         "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:ConditionCheckItem"],
         "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{TABLE}"},
        {"Sid": "Bedrock", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": [f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:inference-profile/{MODEL_ID}",
                      f"arn:aws:bedrock:*::foundation-model/{model}"]}]}))
    # handoff's consumer policy, verbatim scope: one object, decrypt with the one CMK
    iam.put_role_policy(RoleName=ROLE, PolicyName="schwab-token-consumer", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Sid": "GetTheOneObject", "Effect": "Allow", "Action": "s3:GetObject",
             "Resource": f"arn:aws:s3:::{TOKEN_BUCKET}/{TOKEN_KEY}"},
            {"Sid": "DecryptWithCmk", "Effect": "Allow", "Action": ["kms:Decrypt", "kms:DescribeKey"],
             "Resource": st["kms_key_arn"]}]}))
    save()


def code_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("common.py", "engine.py", "api.py", "portal.html"):
            z.write(HERE / "app" / f, f)
    return buf.getvalue()


def lambdas():
    env = {"Variables": {"TABLE": TABLE, "TOKEN_BUCKET": TOKEN_BUCKET, "TOKEN_KEY": TOKEN_KEY,
                         "ACCOUNT_NUMBER": SCHWAB_ACCOUNT_NUMBER, "EXPIRATION": EXPIRATION, "MODEL_ID": MODEL_ID,
                         "MAX_ORDER_USD": str(MAX_ORDER_USD), "MAX_TOTAL_USD": str(MAX_TOTAL_USD), "DISPLAY_OFFSET": str(DISPLAY_OFFSET),
                         "MAX_ACTIVE_ORDERS": str(MAX_ACTIVE_ORDERS), "MAX_ORDERS_PER_DAY": str(MAX_ORDERS_PER_DAY)}}
    z = code_zip()
    for fn, handler, timeout, mem in ((ENGINE_FN, "engine.handler", 120, 1024), (API_FN, "api.handler", 29, 512)):
        print("== lambda", fn)
        if exists(lam.get_function, FunctionName=fn):
            lam.update_function_code(FunctionName=fn, ZipFile=z)
            lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
            lam.update_function_configuration(FunctionName=fn, Environment=env, Timeout=timeout, MemorySize=mem)
            lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
        else:
            for i in range(6):
                try:
                    lam.create_function(FunctionName=fn, Runtime="python3.13", Role=st["role_arn"], Handler=handler,
                                        Code={"ZipFile": z}, Timeout=timeout, MemorySize=mem, Environment=env,
                                        Architectures=["arm64"], Tags=TAGS)
                    break
                except lam.exceptions.InvalidParameterValueException:
                    time.sleep(8)        # role not yet assumable
            lam.get_waiter("function_active_v2").wait(FunctionName=fn)
        st[fn] = lam.get_function(FunctionName=fn)["Configuration"]["FunctionArn"]
    # one engine run at a time; a missed minute is skipped, not queued
    lam.put_function_concurrency(FunctionName=ENGINE_FN, ReservedConcurrentExecutions=1)
    lam.put_function_event_invoke_config(FunctionName=ENGINE_FN, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=60)
    save()


def schedule():
    print("== schedule", RULE)
    arn = ev.put_rule(Name=RULE, ScheduleExpression="rate(1 minute)", State="ENABLED", Tags=taglist)["RuleArn"]
    exists(lam.remove_permission, FunctionName=ENGINE_FN, StatementId="events")
    lam.add_permission(FunctionName=ENGINE_FN, StatementId="events", Action="lambda:InvokeFunction",
                       Principal="events.amazonaws.com", SourceArn=arn)
    ev.put_targets(Rule=RULE, Targets=[{"Id": "engine", "Arn": st[ENGINE_FN]}])


def http_api():
    print("== HTTP API", HTTP_API)
    api = next((a for a in apigw.get_apis()["Items"] if a["Name"] == HTTP_API), None)
    if not api:
        api = apigw.create_api(Name=HTTP_API, ProtocolType="HTTP", Target=st[API_FN], Tags=TAGS)
    st["api_id"], st["api_endpoint"] = api["ApiId"], api["ApiEndpoint"]
    apigw.update_stage(ApiId=api["ApiId"], StageName="$default",
                       DefaultRouteSettings={"ThrottlingRateLimit": 20, "ThrottlingBurstLimit": 40})
    exists(lam.remove_permission, FunctionName=API_FN, StatementId="apigw")
    lam.add_permission(FunctionName=API_FN, StatementId="apigw", Action="lambda:InvokeFunction",
                       Principal="apigateway.amazonaws.com",
                       SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{api['ApiId']}/*")
    save()


def domain():
    print("== certificate", DOMAIN)
    cert = next((c for c in acm.list_certificates(CertificateStatuses=["PENDING_VALIDATION", "ISSUED"])["CertificateSummaryList"]
                 if c["DomainName"] == DOMAIN), None)
    arn = cert["CertificateArn"] if cert else acm.request_certificate(DomainName=DOMAIN, ValidationMethod="DNS", Tags=taglist)["CertificateArn"]
    for _ in range(30):
        d = acm.describe_certificate(CertificateArn=arn)["Certificate"]
        rec = (d.get("DomainValidationOptions") or [{}])[0].get("ResourceRecord")
        if rec:
            break
        time.sleep(3)
    r53.change_resource_record_sets(HostedZoneId=HOSTED_ZONE_ID, ChangeBatch={"Changes": [{"Action": "UPSERT",
        "ResourceRecordSet": {"Name": rec["Name"], "Type": rec["Type"], "TTL": 300, "ResourceRecords": [{"Value": rec["Value"]}]}}]})
    print("   waiting for validation…")
    acm.get_waiter("certificate_validated").wait(CertificateArn=arn, WaiterConfig={"Delay": 15, "MaxAttempts": 80})
    st["cert_arn"] = arn

    print("== custom domain")
    dn = exists(apigw.get_domain_name, DomainName=DOMAIN)
    if not dn:
        dn = apigw.create_domain_name(DomainName=DOMAIN, Tags=TAGS, DomainNameConfigurations=[{
            "CertificateArn": arn, "EndpointType": "REGIONAL", "SecurityPolicy": "TLS_1_2"}])
    cfg = dn["DomainNameConfigurations"][0]
    if not apigw.get_api_mappings(DomainName=DOMAIN)["Items"]:
        apigw.create_api_mapping(DomainName=DOMAIN, ApiId=st["api_id"], Stage="$default")
    r53.change_resource_record_sets(HostedZoneId=HOSTED_ZONE_ID, ChangeBatch={"Changes": [{"Action": "UPSERT",
        "ResourceRecordSet": {"Name": DOMAIN, "Type": "A", "AliasTarget": {
            "HostedZoneId": cfg["HostedZoneId"], "DNSName": cfg["ApiGatewayDomainName"], "EvaluateTargetHealth": False}}}]})
    save()


if __name__ == "__main__":
    if "--code" in sys.argv:
        lambdas()
    else:
        token_side(); table(); role(); lambdas(); schedule(); http_api()
        if DOMAIN:      # optional; without it the portal is at api_endpoint
            domain()
    print(json.dumps({k: v for k, v in st.items()}, indent=1))
