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
API_KEY = os.getenv("API_KEY")  # Reemplaza con tu API Key    
CUSTOM_PASSWORD = os.getenv("CUSTOM_PASSWORD")  # Reemplaza con tu contraseña personalizada    
ACCOUNT_ID = os.getenv("ACCOUNT_ID")  # Reemplaza con tu Account ID    

# Máximo de 2 compras y 2 ventas por símbolo
MAX_TRADES_PER_TYPE = 2

# Configuración de Google Drive
SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")  # Cargar credenciales desde la variable de entorno

# Verificar y convertir la cadena JSON a un diccionario
try:
    SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_CREDENTIALS)  # Convertir la cadena JSON a un diccionario
except json.JSONDecodeError as e:
    raise ValueError(f"Error al decodificar GOOGLE_CREDENTIALS: {e}")

FOLDER_ID = "1bKPwlyVt8a-EizPOTJYDioFNvaWqKja3"  # Reemplaza con el ID de la carpeta en Google Drive
FILE_NAME = "last_signal_4h.json"  # Nombre del archivo en Google Drive

# Autenticación con Google Drive
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
service = build("drive", "v3", credentials=creds)

# Subir un archivo a Google Drive
def upload_file(file_path, file_name):
    # Buscar el archivo existente
    query = f"name='{file_name}' and '{FOLDER_ID}' in parents"
    results = service.files().list(q=query, fields="files(id)").execute()
    items = results.get("files", [])

    if items:
        # Si el archivo existe, actualizarlo
        file_id = items[0]["id"]
        media = MediaFileUpload(file_path, mimetype="application/json")
        file = service.files().update(fileId=file_id, media_body=media).execute()
    else:
        # Si el archivo no existe, crearlo
        file_metadata = {
            "name": file_name,
            "parents": [FOLDER_ID]
        }
        media = MediaFileUpload(file_path, mimetype="application/json")
        file = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    
    return file.get("id")

# Descargar un archivo desde Google Drive
def download_file(file_name):
    query = f"name='{file_name}' and '{FOLDER_ID}' in parents"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get("files", [])

    if not items:
        return None

    file_id = items[0]["id"]
    request = service.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()

    fh.seek(0)
    return json.loads(fh.read().decode("utf-8"))

# Guardar datos en Google Drive
def save_signal(data):
    with open(FILE_NAME, "w") as f:
        json.dump(data, f)
    upload_file(FILE_NAME, FILE_NAME)

# Cargar datos desde Google Drive
def load_signal():
    data = download_file(FILE_NAME)
    if data is None:
        return {}
    return data

# Modelo para validar la entrada
class Signal(BaseModel):
    action: str
    symbol: str
    quantity: int = 1
    timeframe: str = "1m"  # Se agrega timeframe para diferenciar señales

# Endpoint para recibir alertas de TradingView
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Datos crudos recibidos:", data)
    
    try:
        signal = Signal(**data)
        print("Datos recibidos y parseados:", signal)
        
        action = signal.action.lower()
        symbol = signal.symbol
        quantity = signal.quantity
        timeframe = signal.timeframe
        
        # Cargar last_signal_4h desde Google Drive
        last_signal_4h = load_signal()  # Cargar datos desde Google Drive
        
        # Si la señal es de 4H, actualizar la última señal para el símbolo
        if timeframe == "4h":
            print(f"Actualizando última señal de 4H para {symbol}: {action}")
            last_signal_4h[symbol] = action
            save_signal(last_signal_4h)  # Guardar datos en Google Drive
            return {"message": f"Última señal de 4H registrada para {symbol}: {action}"}
        
        # Si la señal es de otro timeframe, verificar la tendencia de 4H
        if symbol in last_signal_4h and last_signal_4h[symbol] != action:
            return {"message": f"Operación bloqueada, la última señal de 4H es {last_signal_4h[symbol]}"}
        
        # Autenticar y obtener los tokens (CST y X-SECURITY-TOKEN)
        cst, x_security_token = authenticate()
        
        # Obtener el número de operaciones activas para el símbolo
        active_trades = get_active_trades(cst, x_security_token, symbol)
        
        if active_trades[action] >= MAX_TRADES_PER_TYPE:
            return {"message": f"Límite de {MAX_TRADES_PER_TYPE} operaciones {action} alcanzado para {symbol}"}
        
        # Ejecutar la orden en Capital.com
        place_order(cst, x_security_token, action.upper(), symbol, quantity)
        
        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Función para autenticar en Capital.com
def authenticate():
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "identifier": ACCOUNT_ID,
        "password": CUSTOM_PASSWORD
    }
    response = requests.post(f"{CAPITAL_API_URL}/session", headers=headers, json=payload)
    
    print("Respuesta completa de autenticación:", response.text)
    
    if response.status_code != 200:
        raise Exception(f"Error de autenticación: {response.text}")
    
    cst = response.headers.get("CST")
    x_security_token = response.headers.get("X-SECURITY-TOKEN")
    
    if not cst or not x_security_token:
        raise Exception("No se encontraron los tokens necesarios (CST, X-SECURITY-TOKEN).")
    
    return cst, x_security_token

# Función para obtener las operaciones activas por símbolo
def get_active_trades(cst: str, x_security_token: str, symbol: str):
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": cst,
        "X-SECURITY-TOKEN": x_security_token,
        "Content-Type": "application/json"
    }
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Error al obtener posiciones abiertas: {response.text}")
    
    positions = response.json().get("positions", [])
    
    trade_count = {"buy": 0, "sell": 0}
    for position in positions:
        if position["market"]["epic"] == symbol:
            direction = position["position"]["direction"].lower()
            trade_count[direction] += 1
    
    return trade_count

# Función para ejecutar una orden en Capital.com
def place_order(cst: str, x_security_token: str, direction: str, epic: str, size: int = 1):
    MIN_SIZE = 1
    if size < MIN_SIZE:
        raise Exception(f"El tamaño mínimo de la orden es {MIN_SIZE}. Estás intentando operar con un tamaño de {size}.")
    
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": cst,
        "X-SECURITY-TOKEN": x_security_token,
        "Content-Type": "application/json"
    }
    
    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "type": "MARKET",
        "currencyCode": "USD"
    }

    response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload)
    
    if response.status_code != 200:
        raise Exception(f"Error al ejecutar la orden: {response.text}")
    
    return response.json()