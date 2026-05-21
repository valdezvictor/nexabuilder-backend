"""
Correct Twilio AccessToken generation.
For browser clients, Twilio requires:
  - account_sid: your AC... value
  - api_key_sid: create at console.twilio.com/project/api-keys  (SK...)
  - api_key_secret: the secret shown once when you create the key
  
For testing without API Keys, we use the account SID + auth token workaround.
"""
import os, time
from functools import lru_cache

@lru_cache(maxsize=1)
def get_twilio_config() -> dict:
    import boto3
    region = os.getenv("AWS_REGION", "us-west-1")
    try:
        ssm = boto3.client("ssm", region_name=region)
        def get(name):
            try:
                return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
            except:
                return ""
        account_sid   = get("/nexabuilder/twilio/ACCOUNT_SID")
        auth_token    = get("/nexabuilder/twilio/AUTH_TOKEN")
        phone_number  = get("/nexabuilder/twilio/PHONE_NUMBER")
        twiml_app_sid = get("/nexabuilder/twilio/TWIML_APP_SID")
        # Optional API Keys (preferred for production)
        api_key_sid    = get("/nexabuilder/twilio/API_KEY_SID")    or ""
        api_key_secret = get("/nexabuilder/twilio/API_KEY_SECRET") or ""
        return {
            "configured":      bool(account_sid and account_sid.startswith("AC")),
            "account_sid":     account_sid,
            "auth_token":      auth_token,
            "phone_number":    phone_number or "+15625125744",
            "twiml_app_sid":   twiml_app_sid,
            "api_key_sid":     api_key_sid,
            "api_key_secret":  api_key_secret,
        }
    except Exception as e:
        print(f"[Twilio] SSM load failed: {e}")
        return {"configured": False, "account_sid": "", "auth_token": "",
                "phone_number": "+15625125744", "twiml_app_sid": "",
                "api_key_sid": "", "api_key_secret": ""}


def generate_voice_token(identity: str) -> dict:
    """
    Generate a Twilio Voice access token for the browser softphone.
    Returns {"token": str, "configured": bool, "error": str|None}
    """
    config = get_twilio_config()

    if not config["configured"]:
        return {"token": None, "configured": False,
                "error": "Twilio credentials not configured"}

    try:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant

        account_sid   = config["account_sid"]
        twiml_app_sid = config["twiml_app_sid"]

        # Use API Keys if available (recommended), else fall back to auth token
        if config["api_key_sid"] and config["api_key_secret"]:
            api_key_sid    = config["api_key_sid"]
            api_key_secret = config["api_key_secret"]
        else:
            # Without API keys: use account_sid as key_sid and auth_token as secret
            # This works for testing but Twilio recommends API keys for production
            api_key_sid    = account_sid
            api_key_secret = config["auth_token"]

        token = AccessToken(
            account_sid,
            api_key_sid,
            api_key_secret,
            identity=identity,
            ttl=3600
        )

        grant = VoiceGrant(
            outgoing_application_sid=twiml_app_sid,
            incoming_allow=True,
        )
        token.add_grant(grant)

        jwt = token.to_jwt()
        jwt_str = jwt if isinstance(jwt, str) else jwt.decode("utf-8")

        return {
            "token":        jwt_str,
            "configured":   True,
            "phone_number": config["phone_number"],
            "identity":     identity,
            "using_api_keys": bool(config["api_key_sid"]),
            "error":        None,
        }

    except Exception as e:
        return {
            "token":      None,
            "configured": False,
            "error":      str(e),
            "phone_number": config["phone_number"],
        }


async def send_sms(to: str, body: str) -> dict:
    config = get_twilio_config()
    if config["configured"]:
        try:
            from twilio.rest import Client
            client = Client(config["account_sid"], config["auth_token"])
            message = client.messages.create(
                body=body, from_=config["phone_number"], to=to)
            print(f"[Twilio SMS] → {to}: {message.sid}")
            return {"success": True, "provider": "twilio", "sid": message.sid}
        except Exception as e:
            print(f"[Twilio SMS] Error: {e}")
    try:
        import boto3
        sns = boto3.client("sns", region_name="us-east-1")
        r = sns.publish(PhoneNumber=to, Message=body)
        return {"success": True, "provider": "sns", "sid": r.get("MessageId","")}
    except Exception as e:
        return {"success": False, "provider": "none", "error": str(e)}
