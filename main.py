from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests
import json
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from io import BytesIO
import time

app = FastAPI()

CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = os.getenv("API_KEY")
CUSTOM_PASSWORD = os.getenv("CUSTOM_PASSWORD")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")

# Configuración de Telegram
TELEGRAM_TOKEN = "tu-telegram-token-aqui"  # Reemplaza con el token de tu bot
TELEGRAM_CHAT_ID = "tu-chat-id-aqui"       # Reemplaza con el chat ID de tu grupo

open_positions = {}

SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

try:
    SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_CREDENTIALS)
except json.JSONDecodeError as e:
    raise ValueError(f"Error al decodificar GOOGLE_CREDENTIALS: {e}")

FOLDER_ID = "1bKPwlyVt8a-EizPOTJYDioFNvaWqKja3"
FILE_NAME = "last_signal_15m.json"
POSITIONS_FILE_NAME = "open_positions.json"  # Nuevo archivo para guardar posiciones abiertas

creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
service = build("drive", "v3", credentials=creds)

def send_telegram_message(message):
    """Envía un mensaje a Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"Error al enviar mensaje a Telegram: {response.text}")
    except Exception as e:
        print(f"Error al enviar mensaje a Telegram: {str(e)}")

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

def save_positions(data):
    with open(POSITIONS_FILE_NAME, "w") as f:
        json.dump(data, f)
    upload_file(POSITIONS_FILE_NAME, POSITIONS_FILE_NAME)

def load_positions():
    return download_file(POSITIONS_FILE_NAME)

# Sincronizar posiciones al iniciar la aplicación
@app.on_event("startup")
async def startup_event():
    global open_positions
    open_positions = load_positions()
    cst, x_security_token = authenticate()
    sync_open_positions(cst, x_security_token)
    send_telegram_message("🚀 Bot iniciado correctamente.")

class Signal(BaseModel):
    action: str
    symbol: str
    quantity: float = 10000.0
    source: str = "rsi"
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
    min_stop_distance = details["dealingRules"]["minStopOrProfitDistance"]["value"] if "minStopOrProfitDistance" in details["dealingRules"] else 0.01  # Valor por defecto 0.01%
    max_stop_distance = details["dealingRules"]["maxStopOrProfitDistance"]["value"] if "maxStopOrProfitDistance" in details["dealingRules"] else None
    return min_size, current_bid, current_offer, min_stop_distance, max_stop_distance

def get_position_details(cst: str, x_security_token: str, epic: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener posiciones: {response.text}")
    positions = response.json().get("positions", [])
    for position in positions:
        if position["market"]["epic"] == epic:
            return {
                "dealId": position["position"]["dealId"],
                "direction": position["position"]["direction"],
                "entry_price": float(position["position"]["level"]),
                "stop_loss": float(position["position"].get("stopLevel", None)) if "stopLevel" in position["position"] else None,  # Manejar la falta de stopLevel
                "quantity": float(position["position"]["size"])
            }
    return None

def sync_open_positions(cst: str, x_security_token: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al sincronizar posiciones: {response.text}")
    positions = response.json().get("positions", [])
    synced_positions = {}
    for pos in positions:
        epic = pos["market"]["epic"]
        try:
            stop_level = float(pos["position"].get("stopLevel", None)) if "stopLevel" in pos["position"] else None
        except (KeyError, TypeError):
            stop_level = None  # Manejar cualquier error al acceder a stopLevel
            print(f"Advertencia: No se encontró stopLevel para posición en {epic}, usando None")
        synced_positions[epic] = {
            "direction": pos["position"]["direction"],
            "entry_price": float(pos["position"]["level"]),
            "stop_loss": stop_level,
            "dealId": pos["position"]["dealId"],
            "quantity": float(pos["position"]["size"])
        }
    
    global open_positions
    open_positions = synced_positions  # Actualizar globalmente sin abrir operaciones automáticamente
    save_positions(open_positions)  # Guardar en Google Drive
    print(f"Posiciones sincronizadas: {json.dumps(open_positions, indent=2)}")

def calculate_valid_stop_loss(entry_price, direction, min_stop_distance, max_stop_distance=None):
    # Redondear entry_price a 5 decimales para USDMXN/EURUSD
    entry_price = round(entry_price, 5)
    if min_stop_distance <= 0:
        min_stop_distance = 0.01  # Valor por defecto 0.01%
    min_stop_value = entry_price * (min_stop_distance / 100) if isinstance(min_stop_distance, float) else 0.01  # Convertir % a valor absoluto
    if max_stop_distance:
        max_stop_value = entry_price * (max_stop_distance / 100) if isinstance(max_stop_distance, float) else None
    
    # Cantidad fija de pérdida: $5 USD con quantity=10,000 y apalancamiento=100
    quantity = 10000.0  # Cantidad fija por operación
    leverage = 100.0    # Apalancamiento 100:1
    loss_amount_usd = 5.0  # Pérdida fija de $5 USD por operación
    
    # Calcular el cambio en el precio para una pérdida de $5 USD
    price_change = (loss_amount_usd * leverage) / quantity  # Cambio en el precio por $5 USD de pérdida
    
    if direction == "BUY":
        stop_loss = round(entry_price - price_change, 5)  # Redondear a 5 decimales
        # Asegurar que el stop loss cumpla con min_stop_distance (no más cerca del precio de entrada)
        final_stop = max(stop_loss, round(entry_price - min_stop_value, 5))  # Asegurar distancia mínima
        if max_stop_distance and final_stop < (entry_price - round(entry_price * (max_stop_distance / 100), 5)):
            final_stop = entry_price - round(entry_price * (max_stop_distance / 100), 5)  # Asegurar distancia máxima
        print(f"Stop Loss para BUY calculado: {final_stop}, entry_price: {entry_price}, loss_amount_usd: {loss_amount_usd}, price_change: {price_change}, min_stop: {min_stop_value}, max_stop: {max_stop_value}")
        return final_stop
    else:
        stop_loss = round(entry_price + price_change, 5)  # Redondear a 5 decimales
        # Asegurar que el stop loss cumpla con min_stop_distance (no más cerca del precio de entrada)
        final_stop = min(stop_loss, round(entry_price + min_stop_value, 5))  # Asegurar distancia mínima
        if max_stop_distance and final_stop > (entry_price + round(entry_price * (max_stop_distance / 100), 5)):
            final_stop = entry_price + round(entry_price * (max_stop_distance / 100), 5)  # Asegurar distancia máxima
        print(f"Stop Loss para SELL calculado: {final_stop}, entry_price: {entry_price}, loss_amount_usd: {loss_amount_usd}, price_change: {price_change}, min_stop: {min_stop_value}, max_stop: {max_stop_value}")
        return final_stop

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
        
        sync_open_positions(cst, x_security_token)
        
        min_size, current_bid, current_offer, min_stop_distance, max_stop_distance = get_market_details(cst, x_security_token, symbol)
        adjusted_quantity = max(quantity, min_size)
        if adjusted_quantity != quantity:
            print(f"Ajustando quantity de {quantity} a {adjusted_quantity} para cumplir con el tamaño mínimo")
        
        entry_price = current_bid if action == "buy" else current_offer
        # Redondear entry_price a 5 decimales
        entry_price = round(entry_price, 5)
        initial_stop_loss = calculate_valid_stop_loss(entry_price, action.upper(), min_stop_distance, max_stop_distance)
        
        print(f"Stop Loss calculado: {initial_stop_loss} para entrada a {entry_price}")
        
        active_trades = get_active_trades(cst, x_security_token, symbol)
        if active_trades["buy"] > 0 or active_trades["sell"] > 0:
            if symbol in open_positions:
                pos = open_positions[symbol]
                opposite_action = "sell" if pos["direction"] == "BUY" else "buy"
                if action == opposite_action:
                    print(f"Intentando cerrar posición para {symbol} con dealId: {pos['dealId']}")
                    try:
                        close_position(cst, x_security_token, pos["dealId"], symbol, adjusted_quantity)
                        print(f"Posición cerrada para {symbol} por señal opuesta")
                        send_telegram_message(f"🔒 Posición cerrada para {symbol}: {pos['direction']} a {pos['entry_price']}")
                        del open_positions[symbol]
                        
                        # Verificar si hay posiciones abiertas antes de abrir una nueva
                        new_active_trades = get_active_trades(cst, x_security_token, symbol)
                        if new_active_trades["buy"] == 0 and new_active_trades["sell"] == 0:
                            deal_ref = place_order(cst, x_security_token, action.upper(), symbol, adjusted_quantity, initial_stop_loss)
                            deal_id = get_position_deal_id(cst, x_security_token, symbol, action.upper())
                            print(f"Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL {initial_stop_loss}, dealId: {deal_id}")
                            send_telegram_message(f"📈 Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL {initial_stop_loss} (dealId: {deal_id})")
                            open_positions[symbol] = {
                                "direction": action.upper(),
                                "entry_price": entry_price,
                                "stop_loss": initial_stop_loss,
                                "dealId": deal_id,
                                "quantity": adjusted_quantity
                            }
                            save_positions(open_positions)  # Guardar estado actualizado
                            return {"message": f"Posición cerrada y nueva orden {action.upper()} ejecutada para {symbol}"}
                        else:
                            raise Exception(f"No se pudo abrir la nueva orden: aún hay posiciones abiertas para {symbol}")
                    except Exception as e:
                        print(f"Error al cerrar posición o abrir nueva: {e}")
                        send_telegram_message(f"❌ Error al cerrar posición o abrir nueva para {symbol}: {str(e)}")
                        raise HTTPException(status_code=500, detail=str(e))
            print(f"Operación rechazada: Ya hay una operación abierta para {symbol}")
            send_telegram_message(f"⚠️ Operación rechazada para {symbol}: Ya hay una operación abierta")
            return {"message": f"Operación rechazada: Ya hay una operación abierta para {symbol}"}
        
        deal_ref = place_order(cst, x_security_token, action.upper(), symbol, adjusted_quantity, initial_stop_loss)
        deal_id = get_position_deal_id(cst, x_security_token, symbol, action.upper())
        print(f"Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL {initial_stop_loss}, dealId: {deal_id}")
        send_telegram_message(f"📈 Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL {initial_stop_loss} (dealId: {deal_id})")
        
        open_positions[symbol] = {
            "direction": action.upper(),
            "entry_price": entry_price,
            "stop_loss": initial_stop_loss,
            "dealId": deal_id,
            "quantity": adjusted_quantity
        }
        save_positions(open_positions)  # Guardar estado actualizado
        
        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        print(f"Error en la ejecución: {e}")
        send_telegram_message(f"❌ Error en la ejecución: {str(e)}")
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

def get_position_deal_id(cst: str, x_security_token: str, epic: str, direction: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener posiciones: {response.text}")
    positions = response.json().get("positions", [])
    for position in positions:
        if position["market"]["epic"] == epic and position["position"]["direction"] == direction:
            return position["position"]["dealId"]
    raise Exception(f"No se encontró posición activa para {epic} en dirección {direction}")

def place_order(cst: str, x_security_token: str, direction: str, epic: str, size: float, stop_loss: float = None):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "type": "MARKET",
        "currencyCode": "USD"
    }
    if stop_loss is not None:
        if not isinstance(stop_loss, (int, float)) or stop_loss <= 0:
            print(f"Advertencia: stop_loss inválido ({stop_loss}), omitiendo stopLevel")
        else:
            # Asegurar exactamente 5 decimales para USDMXN/EURUSD y verificar límites
            payload["stopLevel"] = round(stop_loss, 5)
            print(f"Enviando stopLevel: {payload['stopLevel']} para {epic}")
    
    # Intentar enviar la orden con depuración
    try:
        response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload, timeout=10)
        if response.status_code != 200:
            error_msg = response.text
            print(f"Error en place_order con stopLevel: {error_msg}")
            raise Exception(f"Error al ejecutar la orden: {error_msg}")
    except Exception as e:
        raise Exception(f"Error al ejecutar la orden: {str(e)}")
    
    response_json = response.json()
    print(f"Respuesta completa de place_order: {json.dumps(response_json, indent=2)}")
    deal_key = "dealReference" if "dealReference" in response_json else "dealId"
    if deal_key not in response_json:
        print(f"Respuesta inesperada: {response_json}")
        raise Exception(f"No se encontró '{deal_key}' en la respuesta: {response_json}")
    return response_json[deal_key]

def update_stop_loss(cst: str, x_security_token: str, deal_id: str, new_stop_loss: float):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    payload = {"stopLevel": round(new_stop_loss, 5)}  # Redondear a 5 decimales
    response = requests.put(f"{CAPITAL_API_URL}/positions/{deal_id}", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al actualizar stop loss: {response.text}")

def close_position(cst: str, x_security_token: str, deal_id: str, epic: str, size: float):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    try:
        response = requests.delete(f"{CAPITAL_API_URL}/positions/{deal_id}", headers=headers, timeout=10)
        if response.status_code != 200:
            error_msg = response.text
            print(f"Error en close_position: {error_msg}")
            raise Exception(f"Error al cerrar posición: {error_msg}")
    except Exception as e:
        raise Exception(f"Error al cerrar posición: {str(e)}")
    print(f"Posición cerrada exitosamente para dealId: {deal_id}")

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
            quantity = pos["quantity"]
            leverage = 100.0  # Apalancamiento 100:1
            loss_amount_usd = 5.0  # Pérdida fija de $5 USD por operación
            price_change = (loss_amount_usd * leverage) / quantity
            trailing_stop = round(max_price - price_change, 5)  # Redondear a 5 decimales
            if trailing_stop > pos["stop_loss"]:
                update_stop_loss(cst, x_security_token, pos["dealId"], trailing_stop)
                pos["stop_loss"] = trailing_stop
                print(f"Trailing stop actualizado para {symbol}: {trailing_stop}")
                send_telegram_message(f"🔄 Trailing stop actualizado para {symbol} (BUY): {trailing_stop}")
        else:  # SELL
            min_price = min(pos["entry_price"], current_price)
            quantity = pos["quantity"]
            leverage = 100.0  # Apalancamiento 100:1
            loss_amount_usd = 5.0  # Pérdida fija de $5 USD por operación
            price_change = (loss_amount_usd * leverage) / quantity
            trailing_stop = round(min_price + price_change, 5)  # Redondear a 5 decimales
            if trailing_stop < pos["stop_loss"]:
                update_stop_loss(cst, x_security_token, pos["dealId"], trailing_stop)
                pos["stop_loss"] = trailing_stop
                print(f"Trailing stop actualizado para {symbol}: {trailing_stop}")
                send_telegram_message(f"🔄 Trailing stop actualizado para {symbol} (SELL): {trailing_stop}")
        
        save_positions(open_positions)  # Guardar estado actualizado
        return {"message": f"Trailing stop actualizado para {symbol}"}
    except Exception as e:
        print(f"Error al actualizar trailing stop: {e}")
        send_telegram_message(f"❌ Error al actualizar trailing stop para {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))