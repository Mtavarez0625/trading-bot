import requests
from datetime import datetime

API_URL = "http://127.0.0.1:8000/trade-watchlist"

try:
    response = requests.post(API_URL)
    print("Ran at:", datetime.now().isoformat())
    print("Status code:", response.status_code)
    print("Response:", response.json())
except Exception as e:
    print("Error:", str(e))