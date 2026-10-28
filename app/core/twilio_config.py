"""
app/core/twilio_config.py
==========================
Twilio configuration — loads credentials from AWS SSM Parameter Store.
"""
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def get_twilio_config() -> dict:
    """Load Twilio config from SSM. Cached after first load."""
    import boto3
    region = os.getenv("AWS_REGION", "us-west-1")
    try:
        ssm = boto3.client("ssm", region_name=region)

        def get(name: str) -> str:
            try:
                return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
            except Exception:
                return ""

        account_sid    = get("/nexabuilder/twilio/ACCOUNT_SID")
        auth_token     = get("/nexabuilder/twilio/AUTH_TOKEN")
        phone_number   = get("/nexabuilder/twilio/PHONE_NUMBER")
        twiml_app_sid  = get("/nexabuilder/twilio/TWIML_APP_SID")
        api_key_sid    = get("/nexabuilder/twilio/API_KEY_SID")
        api_key_secret = get("/nexabuilder/twilio/API_KEY_SECRET")

        configured = bool(account_sid and account_sid.startswith("AC"))
        has_api_keys = bool(api_key_sid and api_key_sid.startswith("SK") and api_key_secret)

        return {
            "configured":      configured,
            "has_api_keys":    has_api_keys,
            "account_sid":     account_sid,
            "auth_token":      auth_token,
            "phone_number":    phone_number or "+15625125744",
            "twiml_app_sid":   twiml_app_sid,
            "api_key_sid":     api_key_sid,
            "api_key_secret":  api_key_secret,
        }
    except Exception as e:
        print(f"[Twilio] SSM load failed: {e}")
        return {
            "configured": False, "has_api_keys": False,
            "account_sid": "", "auth_token": "",
            "phone_number": "+15625125744", "twiml_app_sid": "",
            "api_key_sid": "", "api_key_secret": "",
        }


def generate_voice_token(identity: str) -> dict:
    """
    Generate a Twilio Voice access token for the browser softphone.

    REQUIRES Twilio API Keys (SK... format) stored in SSM:
      /nexabuilder/twilio/API_KEY_SID    → SK...
      /nexabuilder/twilio/API_KEY_SECRET → secret

    Create at: console.twilio.com → Account → API keys & tokens → Create API key
    Then store with:
      aws ssm put-parameter --name /nexabuilder/twilio/API_KEY_SID --value SK... --type SecureString --region us-west-1
      aws ssm put-parameter --name /nexabuilder/twilio/API_KEY_SECRET --value ... --type SecureString --region us-west-1

    Returns {"token": str, "configured": bool, "has_api_keys": bool, "error": str|None}
    """
    config = get_twilio_config()

    if not config["configured"]:
        return {"token": None, "configured": False, "has_api_keys": False,
                "error": "Twilio credentials not configured in SSM"}

    if not config["has_api_keys"]:
        return {
            "token":        None,
            "configured":   True,
            "has_api_keys": False,
            "phone_number": config["phone_number"],
            "error":        (
                "Twilio API Keys required for browser calling. "
                "Create at console.twilio.com → Account → API keys & tokens → "
                "Create API key (Standard). Then run: "
                "aws ssm put-parameter --name /nexabuilder/twilio/API_KEY_SID "
                "--value SK... --type SecureString --region us-west-1 && "
                "aws ssm put-parameter --name /nexabuilder/twilio/API_KEY_SECRET "
                "--value ... --type SecureString --region us-west-1"
            )
        }

    try:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant

        token = AccessToken(
            config["account_sid"],
            config["api_key_sid"],      # Must be SK... format
            config["api_key_secret"],
            identity=identity,
            ttl=3600
        )
        grant = VoiceGrant(
            outgoing_application_sid=config["twiml_app_sid"],
            incoming_allow=True,
        )
        token.add_grant(grant)
        jwt = token.to_jwt()
        jwt_str = jwt if isinstance(jwt, str) else jwt.decode("utf-8")

        return {
            "token":        jwt_str,
            "configured":   True,
            "has_api_keys": True,
            "phone_number": config["phone_number"],
            "identity":     identity,
            "error":        None,
        }

    except Exception as e:
        return {
            "token": None, "configured": True, "has_api_keys": True,
            "error": str(e), "phone_number": config["phone_number"],
        }


async def send_sms(to: str, body: str) -> dict:
    """Send SMS via Twilio (preferred) or SNS (fallback)."""
    config = get_twilio_config()
    if config["configured"]:
        try:
            from twilio.rest import Client
            client = Client(config["account_sid"], config["auth_token"])
            msg = client.messages.create(body=body, from_=config["phone_number"], to=to)
            print(f"[Twilio SMS] → {to}: {msg.sid}")
            return {"success": True, "provider": "twilio", "sid": msg.sid}
        except Exception as e:
            print(f"[Twilio SMS] Error: {e}")
    try:
        import boto3
        sns = boto3.client("sns", region_name="us-east-1")
        r = sns.publish(PhoneNumber=to, Message=body)
        return {"success": True, "provider": "sns", "sid": r.get("MessageId", "")}
    except Exception as e:
        return {"success": False, "provider": "none", "error": str(e)}
