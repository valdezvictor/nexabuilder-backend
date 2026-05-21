"""
Twilio configuration — loads credentials from AWS SSM Parameter Store.
Falls back to SNS for SMS if Twilio not configured.
"""
import os
import boto3
from functools import lru_cache

@lru_cache(maxsize=1)
def get_twilio_config() -> dict:
    """Load Twilio config from SSM. Cached after first load."""
    region = os.getenv("AWS_REGION", "us-west-1")
    
    try:
        ssm = boto3.client("ssm", region_name=region)
        
        def get_param(name: str) -> str:
            try:
                r = ssm.get_parameter(Name=name, WithDecryption=True)
                return r["Parameter"]["Value"]
            except Exception:
                return ""
        
        account_sid   = get_param("/nexabuilder/twilio/ACCOUNT_SID")
        auth_token    = get_param("/nexabuilder/twilio/AUTH_TOKEN")
        phone_number  = get_param("/nexabuilder/twilio/PHONE_NUMBER")
        twiml_app_sid = get_param("/nexabuilder/twilio/TWIML_APP_SID")
        
        configured = bool(account_sid and account_sid.startswith("AC"))
        
        return {
            "configured":    configured,
            "account_sid":   account_sid,
            "auth_token":    auth_token,
            "phone_number":  phone_number or "+15625125744",
            "twiml_app_sid": twiml_app_sid,
        }
    except Exception as e:
        print(f"[Twilio] SSM load failed: {e}")
        return {
            "configured":    False,
            "account_sid":   "",
            "auth_token":    "",
            "phone_number":  "+15625125744",
            "twiml_app_sid": "",
        }


async def send_sms(to: str, body: str) -> dict:
    """
    Send SMS via Twilio (preferred) or AWS SNS (fallback).
    Returns {"success": bool, "provider": str, "sid": str}
    """
    config = get_twilio_config()
    
    if config["configured"]:
        # Twilio
        try:
            from twilio.rest import Client
            client = Client(config["account_sid"], config["auth_token"])
            message = client.messages.create(
                body=body,
                from_=config["phone_number"],
                to=to
            )
            print(f"[Twilio SMS] → {to}: {message.sid}")
            return {"success": True, "provider": "twilio", "sid": message.sid}
        except Exception as e:
            print(f"[Twilio SMS] Error: {e}")
            # Fall through to SNS
    
    # AWS SNS fallback
    try:
        import boto3
        sns = boto3.client("sns", region_name="us-east-1")
        response = sns.publish(PhoneNumber=to, Message=body)
        print(f"[SNS SMS] → {to}: {response.get('MessageId','')}")
        return {"success": True, "provider": "sns", "sid": response.get("MessageId", "")}
    except Exception as e:
        print(f"[SNS SMS] Error: {e}")
        return {"success": False, "provider": "none", "sid": "", "error": str(e)}


async def generate_twilio_token(identity: str) -> str:
    """
    Generate a Twilio Voice access token for the browser softphone.
    Call center agents use this to make/receive calls via WebRTC.
    """
    config = get_twilio_config()
    
    if not config["configured"]:
        return ""
    
    try:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant
        
        token = AccessToken(
            config["account_sid"],
            config["auth_token"],     # This should be API Key SID in production
            config["auth_token"],     # This should be API Key Secret in production
            identity=identity,
            ttl=3600
        )
        
        voice_grant = VoiceGrant(
            outgoing_application_sid=config["twiml_app_sid"],
            incoming_allow=True,
        )
        token.add_grant(voice_grant)
        return token.to_jwt()
    except Exception as e:
        print(f"[Twilio Token] Error: {e}")
        return ""
