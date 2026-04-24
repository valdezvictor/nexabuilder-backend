import base64
import json
import os
from dotenv import load_dotenv

load_dotenv()
SECRET = os.getenv("JWT_SECRET")
token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIyMjY1YWI4Yy03Yzk0LTQ5NjQtOWEyYi0yNGQ4NTQwMTgwMWEiLCJ0ZW5hbnQiOiJhMDQwZmUzNi1kYWQwLTQ5NjMtYjhjMi1mYjBhYTgzNGU2ZjciLCJyb2xlIjoiYWRtaW4iLCJleHAiOjE3NzYyMjg3NTV9.xccsX0mb_c7yiRfmifK0_kwI15X-doz16kUqidyFLUY"

print("--- Simple JWT Inspector ---")
try:
    # Split the token into Header, Payload, Signature
    parts = token.split('.')
    if len(parts) != 3:
        print("❌ FAIL: Token is not a valid 3-part JWT.")
    else:
        # Decode the Payload (the middle part)
        payload_b64 = parts[1]
        # Add padding if necessary
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload_data = base64.b64decode(payload_b64).decode('utf-8')
        payload = json.loads(payload_data)
        
        print(f"✅ Decoded Payload: {json.dumps(payload, indent=2)}")
        print(f"\n--- Environment Check ---")
        if SECRET:
            print(f"JWT_SECRET found in .env: {SECRET[:4]}...{SECRET[-4:]}")
        else:
            print("❌ WARNING: No JWT_SECRET found in .env!")

except Exception as e:
    print(f"❌ ERROR: {str(e)}")
