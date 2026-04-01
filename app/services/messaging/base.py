# app/services/messaging/base.py
class SMSProvider(ABC):
    @abstractmethod
    def send_sms(self, to: str, message: str) -> None:
        ...

class EmailProvider(ABC):
    @abstractmethod
    def send_email(self, to: str, subject: str, body: str) -> None:
        ...
