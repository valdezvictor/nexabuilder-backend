# app/services/messaging/email_ses.py
class SESEmailProvider(EmailProvider):
    def __init__(self, region: str, from_address: str):
        self.client = boto3.client("ses", region_name=region)
        self.from_address = from_address

    def send_email(self, to: str, subject: str, body: str) -> None:
        self.client.send_email(
            Source=self.from_address,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
