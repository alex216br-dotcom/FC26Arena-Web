import os
import smtplib
from email.message import EmailMessage
import httpx
from sqlalchemy.orm import Session
from .models import Notification, User

def queue_notification(db: Session, user: User | None, subject: str, body: str, channel: str = "site"):
    note = Notification(user_id=user.id if user else None, subject=subject, body=body, channel=channel, status="sent" if channel == "site" else "pending")
    db.add(note)
    db.commit()

def send_email(to_address: str, subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST")
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM") or username
    if not all([host, username, password, sender, to_address]):
        return False
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)
    return True

def send_telegram(chat_id: str, body: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        return False
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": body},
        timeout=20,
    )
    return response.is_success

def send_whatsapp_webhook(phone: str, subject: str, body: str) -> bool:
    url = os.getenv("WHATSAPP_WEBHOOK_URL")
    if not url:
        return False
    response = httpx.post(url, json={"phone": phone, "subject": subject, "message": body}, timeout=20)
    return response.is_success

def notify_user(db: Session, user: User, subject: str, body: str):
    queue_notification(db, user, subject, body, "site")
    if user.notify_email and user.email:
        sent = send_email(user.email, subject, body)
        queue_notification(db, user, subject, body, "email")
        db.query(Notification).order_by(Notification.id.desc()).first().status = "sent" if sent else "pending"
        db.commit()
    if user.notify_telegram and user.telegram_chat_id:
        sent = send_telegram(user.telegram_chat_id, body)
        queue_notification(db, user, subject, body, "telegram")
        db.query(Notification).order_by(Notification.id.desc()).first().status = "sent" if sent else "pending"
        db.commit()
    if user.notify_whatsapp:
        sent = send_whatsapp_webhook(user.whatsapp, subject, body)
        queue_notification(db, user, subject, body, "whatsapp")
        db.query(Notification).order_by(Notification.id.desc()).first().status = "sent" if sent else "pending"
        db.commit()
