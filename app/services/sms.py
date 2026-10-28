# app/services/sms.py
import boto3
from botocore.exceptions import ClientError

def send_sms(phone_number: str, message: str) -> bool:
    if not phone_number.startswith("+"):
        print(f"[SMS] Invalid phone: {phone_number}")
        return False
    try:
        sns = boto3.client('sns', region_name='us-east-1')
        response = sns.publish(
            PhoneNumber=phone_number,
            Message=message,
            MessageAttributes={
                'AWS.SNS.SMS.SMSType': {'DataType': 'String', 'StringValue': 'Transactional'},
                'AWS.SNS.SMS.SenderID': {'DataType': 'String', 'StringValue': 'NexaBld'}
            }
        )
        print(f'[SMS SENT] To: {phone_number} | MessageId: {response["MessageId"]}')
        return True
    except ClientError as e:
        print(f'[SMS FALLBACK] {e.response["Error"]["Code"]}: {e.response["Error"]["Message"]}')
        print(f'[SMS CONSOLE] To: {phone_number} | {message}')
        return False
    except Exception as e:
        print(f'[SMS ERROR] {e} | To: {phone_number}')
        return False

def send_magic_link_sms(phone_number: str, token_url: str) -> bool:
    message = 'Your NexaBuilder project is ready. Access here: ' + token_url + ' (Valid 30 days)'
    return send_sms(phone_number, message)
