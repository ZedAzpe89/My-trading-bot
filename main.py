from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests
import json
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from io import BytesIO

app = FastAPI()

# Configuración de la API de Capital.com
CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = os.getenv("API_KEY")
CUSTOM_PASSWORD = os.getenv("CUSTOM_PASSWORD")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")

MAX_TRADES_PER_TYPE = 2

# Configuración de Google Drive
SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

try:
    SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_CREDENTIALS)
except json.JSONDecodeError as e:
    raise ValueError(f"Error al decodificar GOOGLE_CREDENTIALS: {e}")

FOLDER_ID = "1bKPwlyVt8a-EizPOTJYDioFNvaWqKja3"
FILE_NAME = "last_signal_15m.json"

creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
service = build("drive", "v3", credentials=creds)

def upload_file(file_path, file_name):
    query = f"name='{file_name}' and '{FOLDER_ID}' in parents"
    results = service.files().list(q=query, fields="files(id)").execute()
    items = results.get("files", [])

    if items:
        file_id = items[0]["id"]
        media = MediaFileUpload(file_path, mimetype="application/json")
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {"name": file_name, "parents": [FOLDER_ID]}
        media = MediaFileUpload(file_path, mimetype="application/json")
        service.files().create(body=file_metadata, media_body=media, fields="id").execute()

def download_file(file_name):
    query = f"name='{file_name}' and '{FOLDER_ID}' in parents"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get("files", [])

    if not items:
        return {}

    file_id = items[0]["id"]
    request = service.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    
    fh.seek(0)
    return json.loads(fh.read().decode("utf-8"))

def save_signal(data):
    with open(FILE_NAME, "w") as f:
        json.dump(data, f)
    upload_file(FILE_NAME, FILE_NAME)

def load_signal():
    return download_file(FILE_NAME)

class Signal(BaseModel):
    action: str
    symbol: str
    quantity: int = 1
    timeframe: str = "1m"

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Datos crudos recibidos:", data)
    
    try:
        signal = Signal(**data)
        print("Datos recibidos y parseados:", signal)
        
        action, symbol, quantity, timeframe = signal.action.lower(), signal.symbol, signal.quantity, signal.timeframe
        last_signal_15m = load_signal()
        
        if timeframe == "15m":
            print(f"Actualizando última señal de 15m para {symbol}: {action}")
            last_signal_15m[symbol] = action
            save_signal(last_signal_15m)
            return {"message": f"Última señal de 15m registrada para {symbol}: {action}"}
        
        if symbol in last_signal_15m and last_signal_15m[symbol] != action:
            print(f"Operación bloqueada, última señal de 15m: {last_signal_15m[symbol]}")
            return {"message": f"Operación bloqueada, la última señal de 15m es {last_signal_15m[symbol]}"}
        
        cst, x_security_token = authenticate()
        print("Autenticación exitosa en Capital.com")
        
        active_trades = get_active_trades(cst, x_security_token, symbol)
        print(f"Operaciones activas para {symbol}: {active_trades}")
        
        if active_trades[action] >= MAX_TRADES_PER_TYPE:
            print(f"Límite de {MAX_TRADES_PER_TYPE} operaciones {action} alcanzado para {symbol}")
            return {"message": f"Límite de {MAX_TRADES_PER_TYPE} operaciones {action} alcanzado para {symbol}"}
        
        place_order(cst, x_security_token, action.upper(), symbol, quantity)
        print(f"Orden {action.upper()} ejecutada correctamente para {symbol} con cantidad {quantity}")
        
        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        print(f"Error en la ejecución: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def authenticate():
    headers = {"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"}
    payload = {"identifier": ACCOUNT_ID, "password": CUSTOM_PASSWORD}
    response = requests.post(f"{CAPITAL_API_URL}/session", headers=headers, json=payload)
    
    if response.status_code != 200:
        raise Exception(f"Error de autenticación: {response.text}")
    
    return response.headers.get("CST"), response.headers.get("X-SECURITY-TOKEN")

def get_active_trades(cst: str, x_security_token: str, symbol: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Error al obtener posiciones abiertas: {response.text}")
    
    trade_count = {"buy": 0, "sell": 0}
    for position in response.json().get("positions", []):
        if position["market"]["epic"] == symbol:
            trade_count[position["position"]["direction"].lower()] += 1
    
    return trade_count

def place_order(cst: str, x_security_token: str, direction: str, epic: str, size: int = 1):
    if size < 1:
        raise Exception("El tamaño mínimo de la orden es 1.")
    
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    payload = {"epic": epic, "direction": direction, "size": size, "type": "MARKET", "currencyCode": "USD"}
    response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload)
    
    if response.status_code != 200:
        raise Exception(f"Error al ejecutar la orden: {response.text}")