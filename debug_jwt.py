import jwt
import os
from dotenv import load_dotenv

# Load the secret from your .env file
load_dotenv()
SECRET = os.getenv("JWT_SECRET")

# THE TOKEN YOU PASTED EARLIER
token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIyMjY1YWI4Yy03Yzk0LTQ5NjQtOWEyYi0yNGQ4NTQwMTgwMWEiLCJ0ZW5hbnQiOiJhMDQwZmUzNi1kYWQwLTQ5NjMtYjhjMi1mYjBhYTgzNGU2ZjciLCJyb2xlIjoiYWRtaW4iLCJleHAiOjE3NzYyMjg3NTV9.xccsX0mb_c7yiRfmifK0_kwI15X-doz16kUqidyFLUY"

print(f"--- JWT Debugger ---")
print(f"Using Secret: {SECRET[:5]}...{SECRET[-5:]}")

try:
    # Attempt to decode
    decoded = jwt.decode(token, SECRET, algorithms=["HS256"])
    print("\n✅ SUCCESS: The Secret matches the Token!")
    print(f"Payload Content: {decoded}")
except jwt.ExpiredSignatureError:
    print("\n❌ FAIL: Token has expired.")
except jwt.InvalidSignatureError:
    print("\n❌ FAIL: Invalid Signature. The JWT_SECRET in your .env does NOT match the one that signed this token.")
except Exception as e:
    print(f"\n❌ ERROR: {str(e)}")
