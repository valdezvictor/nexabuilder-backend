# app/services/messaging/sms_sns.py
import boto3

class SNSProvider(SMSProvider):
    def __init__(self, region: str):
        self.client = boto3.client("sns", region_name=region)

    def send_sms(self, to: str, message: str) -> None:
        self.client.publish(PhoneNumber=to, Message=message)
