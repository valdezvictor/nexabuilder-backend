# app/services/messaging/registry.py
def get_sms_provider(db) -> SMSProvider:
    # load default sms provider from MessagingProvider table
    provider = (
        db.query(MessagingProvider)
        .filter_by(type=MessagingType.sms, is_default=True, is_active=True)
        .first()
    )
    # for now, assume SNS
    return SNSProvider(region=provider.config["region"])

def get_email_provider(db) -> EmailProvider:
    provider = (
        db.query(MessagingProvider)
        .filter_by(type=MessagingType.email, is_default=True, is_active=True)
        .first()
    )
    return SESEmailProvider(
        region=provider.config["region"],
        from_address=provider.config["from_address"],
    )
