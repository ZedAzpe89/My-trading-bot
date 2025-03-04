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

CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = os.getenv("API_KEY")
CUSTOM_PASSWORD = os.getenv("CUSTOM_PASSWORD")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")

open_positions = {}

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
    quantity: float = 10000.0
    source: str = "ema"
    timeframe: str = "1m"

def get_market_details(cst: str, x_security_token: str, epic: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/markets/{epic}", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener detalles del mercado: {response.text}")
    details = response.json()
    print(f"Respuesta completa de /markets/{epic}: {json.dumps(details, indent=2)}")
    min_size = details["dealingRules"]["minDealSize"]["value"]
    current_bid = details["snapshot"]["bid"]
    current_offer = details["snapshot"]["offer"]
    return min_size, current_bid, current_offer

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Datos crudos recibidos:", data)
    
    try:
        signal = Signal(**data)
        print("Datos recibidos y parseados:", signal.dict())
        
        action, symbol, quantity, source, timeframe = signal.action.lower(), signal.symbol, signal.quantity, signal.source, signal.timeframe
        last_signal_15m = load_signal()
        print(f"Última señal de 15m cargada: {last_signal_15m}")
        
        if timeframe == "15m":
            print(f"Actualizando última señal de 15m para {symbol}: {action}")
            last_signal_15m[symbol] = action
            save_signal(last_signal_15m)
            return {"message": f"Última señal de 15m registrada para {symbol}: {action}"}
        
        cst, x_security_token = authenticate()
        print("Autenticación exitosa en Capital.com")
        
        min_size, current_bid, current_offer = get_market_details(cst, x_security_token, symbol)
        adjusted_quantity = max(quantity, min_size)
        if adjusted_quantity != quantity:
            print(f"Ajustando quantity de {quantity} a {adjusted_quantity} para cumplir con el tamaño mínimo")
        
        entry_price = current_bid if action == "buy" else current_offer
        initial_stop_loss = entry_price * (0.999 if action == "buy" else 1.001)
        
        active_trades = get_active_trades(cst, x_security_token, symbol)
        if active_trades["buy"] > 0 or active_trades["sell"] > 0:
            if symbol in open_positions:
                pos = open_positions[symbol]
                opposite_action = "sell" if pos["direction"] == "BUY" else "buy"
                if action == opposite_action:
                    # Cerrar la posición actual
                    close_position(cst, x_security_token, pos["dealId"], symbol, adjusted_quantity)
                    print(f"Posición cerrada para {symbol} por señal opuesta")
                    del open_positions[symbol]
                    
                    # Abrir nueva posición con la acción de la alerta
                    deal_id = place_order(cst, x_security_token, action.upper(), symbol, adjusted_quantity, initial_stop_loss)
                    print(f"Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL inicial {initial_stop_loss}")
                    open_positions[symbol] = {
                        "direction": action.upper(),
                        "entry_price": entry_price,
                        "stop_loss": initial_stop_loss,
                        "dealId": deal_id
                    }
                    return {"message": f"Posición cerrada y nueva orden {action.upper()} ejecutada para {symbol}"}
            print(f"Operación rechazada: Ya hay una operación abierta para {symbol}")
            return {"message": f"Operación rechazada: Ya hay una operación abierta para {symbol}"}
        
        deal_id = place_order(cst, x_security_token, action.upper(), symbol, adjusted_quantity, initial_stop_loss)
        print(f"Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL inicial {initial_stop_loss}")
        
        open_positions[symbol] = {
            "direction": action.upper(),
            "entry_price": entry_price,
            "stop_loss": initial_stop_loss,
            "dealId": deal_id
        }
        
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
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener posiciones: {response.text}")
    trade_count = {"buy": 0, "sell": 0}
    for position in response.json().get("positions", []):
        if position["market"]["epic"] == symbol:
            trade_count[position["position"]["direction"].lower()] += 1
    return trade_count

def place_order(cst: str, x_security_token: str, direction: str, epic: str, size: float, stop_loss: float = None):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "type": "MARKET",
        "currencyCode": "USD"
    }
    if stop_loss:
        payload["stopLevel"] = stop_loss
    response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload)
    if response.status_code != 200:
        error_msg = response.text
        print(f"Error en place_order: {error_msg}")
        raise Exception(f"Error al ejecutar la orden: {error_msg}")
    response_json = response.json()
    deal_key = "dealReference" if "dealReference" in response_json else "dealId"
    if deal_key not in response_json:
        print(f"Respuesta inesperada: {response_json}")
        raise Exception(f"No se encontró '{deal_key}' en la respuesta: {response_json}")
    return response_json[deal_key]

def update_stop_loss(cst: str, x_security_token: str, deal_id: str, new_stop_loss: float):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    payload = {"stopLevel": new_stop_loss}
    response = requests.put(f"{CAPITAL_API_URL}/positions/{deal_id}", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al actualizar stop loss: {response.text}")

def close_position(cst: str, x_security_token: str, deal_id: str, epic: str, size: float):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    # Usar POST /positions/otc para cerrar la posición con una orden opuesta
    opposite_direction = "SELL" if open_positions[epic]["direction"] == "BUY" else "BUY"
    payload = {
        "dealId": deal_id,
        "epic": epic,
        "direction": opposite_direction,
        "size": size,
        "orderType": "MARKET",
        "level": None,  # Precio de mercado
        "timeInForce": "FILL_OR_KILL"
    }
    response = requests.post(f"{CAPITAL_API_URL}/positions/otc", headers=headers, json=payload)
    if response.status_code != 200:
        error_msg = response.text
        print(f"Error en close_position: {error_msg}")
        raise Exception(f"Error al cerrar posición: {error_msg}")

@app.post("/update_trailing")
async def update_trailing(request: Request):
    try:
        data = await request.json()
        symbol = data["symbol"]
        current_price = float(data["current_price"])
        
        if symbol not in open_positions:
            return {"message": f"No hay posición abierta para {symbol}"}
        
        pos = open_positions[symbol]
        cst, x_security_token = authenticate()
        
        if pos["direction"] == "BUY":
            max_price = max(pos["entry_price"], current_price)
            trailing_stop = max_price * 0.95
            if trailing_stop > pos["stop_loss"]:
                update_stop_loss(cst, x_security_token, pos["dealId"], trailing_stop)
                pos["stop_loss"] = trailing_stop
                print(f"Trailing stop actualizado para {symbol}: {trailing_stop}")
        else:  # SELL
            min_price = min(pos["entry_price"], current_price)
            trailing_stop = min_price * 1.05
            if trailing_stop < pos["stop_loss"]:
                update_stop_loss(cst, x_security_token, pos["dealId"], trailing_stop)
                pos["stop_loss"] = trailing_stop
                print(f"Trailing stop actualizado para {symbol}: {trailing_stop}")
        
        return {"message": f"Trailing stop actualizado para {symbol}"}
    except Exception as e:
        print(f"Error al actualizar trailing stop: {e}")
        raise HTTPException(status_code=500, detail=str(e))