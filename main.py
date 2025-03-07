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
import asyncio
import logging
from contextlib import asynccontextmanager

# Configuración de logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

app = FastAPI()

CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = os.getenv("API_KEY")
CUSTOM_PASSWORD = os.getenv("CUSTOM_PASSWORD")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")

# Configuración de Telegram
TELEGRAM_TOKEN = "7247126230:AAFBj8M6cca3NHcN6rUr0wDNyTZtu8dq-LQ"  # Reemplaza con el token de tu bot
TELEGRAM_CHAT_ID = "-4757476521"       # Reemplaza con el chat ID de tu grupo

# Variables globales
open_positions = {}
cst = None
x_security_token = None

SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

try:
    SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_CREDENTIALS)
except json.JSONDecodeError as e:
    raise ValueError(f"Error al decodificar GOOGLE_CREDENTIALS: {e}")

FOLDER_ID = "1bKPwlyVt8a-EizPOTJYDioFNvaWqKja3"
FILE_NAME = "last_signal_15m.json"
POSITIONS_FILE_NAME = "open_positions.json"

creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
service = build("drive", "v3", credentials=creds)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            logger.error(f"Error al enviar mensaje a Telegram: {response.text}")
    except Exception as e:
        logger.error(f"Error al enviar mensaje a Telegram: {str(e)}")

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
    positions = download_file(POSITIONS_FILE_NAME)
    return positions if positions is not None else {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global open_positions, cst, x_security_token
    logger.setLevel(logging.INFO)  # Nivel INFO para el servicio web
    open_positions = load_positions()
    cst, x_security_token = authenticate()
    sync_open_positions(cst, x_security_token)
    logger.info("🚀 Bot iniciado correctamente.")
    yield

app = FastAPI(lifespan=lifespan)

class Signal(BaseModel):
    action: str
    symbol: str
    quantity: float = 10000.0
    source: str = "rsi"
    timeframe: str = "1m"
    loss_amount_usd: float = 10.0

def authenticate():
    global cst, x_security_token
    headers = {"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"}
    payload = {"identifier": ACCOUNT_ID, "password": CUSTOM_PASSWORD}
    response = requests.post(f"{CAPITAL_API_URL}/session", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error de autenticación: {response.text}")
    cst = response.headers.get("CST")
    x_security_token = response.headers.get("X-SECURITY-TOKEN")
    return cst, x_security_token

def get_market_details(cst: str, x_security_token: str, epic: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/markets/{epic}", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener detalles del mercado: {response.text}")
    details = response.json()
    min_size = details["dealingRules"]["minDealSize"]["value"]
    current_bid = details["snapshot"]["bid"]
    current_offer = details["snapshot"]["offer"]
    spread = current_offer - current_bid
    min_stop_distance = details["dealingRules"]["minStopOrProfitDistance"]["value"] if "minStopOrProfitDistance" in details["dealingRules"] else 0.01
    max_stop_distance = details["dealingRules"]["maxStopOrProfitDistance"]["value"] if "maxStopOrProfitDistance" in details["dealingRules"] else None
    return min_size, current_bid, current_offer, spread, min_stop_distance, max_stop_distance

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
                "stop_loss": float(position["position"].get("stopLevel", None)) if "stopLevel" in position["position"] else None,
                "quantity": float(position["position"]["size"])
            }
    return None

def get_deal_confirmation(cst: str, x_security_token: str, deal_reference: str, retries=3, delay=1):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    for attempt in range(retries):
        response = requests.get(f"{CAPITAL_API_URL}/confirms/{deal_reference}", headers=headers)
        if response.status_code == 200:
            confirmation = response.json()
            if "profit" in confirmation and confirmation["profit"] is not None:
                return confirmation
            elif "level" in confirmation and confirmation["level"] is not None:
                return confirmation
            else:
                logger.warning(f"Advertencia: Campos 'profit' o 'level' no encontrados en la confirmación (intento {attempt + 1}/{retries})")
        else:
            logger.error(f"Error al obtener confirmación (intento {attempt + 1}/{retries}): {response.text}")
        if attempt < retries - 1:
            time.sleep(delay)
    raise Exception(f"No se pudo obtener la confirmación después de {retries} intentos")

def sync_open_positions(cst: str, x_security_token: str):
    global open_positions
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    try:
        response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
        if response.status_code != 200:
            if "errorCode" in response.json() and "invalid.session.token" in response.json()["errorCode"]:
                logger.warning("Token de sesión inválido detectado, intentando reautenticación...")
                global cst, x_security_token
                cst, x_security_token = authenticate()
                headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
                response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
                if response.status_code != 200:
                    raise Exception(f"Error al sincronizar posiciones tras reautenticación: {response.text}")
            else:
                raise Exception(f"Error al sincronizar posiciones: {response.text}")
        positions = response.json().get("positions", [])
        synced_positions = {}
        for pos in positions:
            epic = pos["market"]["epic"]
            try:
                stop_level = float(pos["position"].get("stopLevel", None)) if "stopLevel" in pos["position"] else None
            except (KeyError, TypeError):
                stop_level = None
                logger.warning(f"Advertencia: No se encontró stopLevel para posición en {epic}, usando None")
            synced_positions[epic] = {
                "direction": pos["position"]["direction"],
                "entry_price": float(pos["position"]["level"]),
                "stop_loss": stop_level,
                "dealId": pos["position"]["dealId"],
                "quantity": float(pos["position"]["size"])
            }
        
        closed_positions = {k: v for k, v in open_positions.items() if k not in synced_positions}
        for symbol, pos in closed_positions.items():
            if pos["stop_loss"] and (pos["direction"] == "BUY" and pos["stop_loss"] >= pos["entry_price"]) or (pos["direction"] == "SELL" and pos["stop_loss"] <= pos["entry_price"]):
                profit_loss = calculate_profit_loss_from_stop_loss(pos)
                profit_loss_message = f"+${profit_loss} USD" if profit_loss >= 0 else f"-${abs(profit_loss)} USD"
                send_telegram_message(f"🔒 Posición cerrada por stop loss para {symbol}: {pos['direction']} a {pos['entry_price']}. Ganancia/pérdida: {profit_loss_message}")
                logger.info(f"Posición cerrada por stop loss para {symbol}, profit_loss: {profit_loss} USD")
        
        open_positions = synced_positions
        save_positions(open_positions)
    except Exception as e:
        logger.error(f"Error en sync_open_positions: {e}")
        raise

def calculate_valid_stop_loss(entry_price, direction, loss_amount_usd, quantity, leverage, min_stop_distance, max_stop_distance=None):
    entry_price = round(entry_price, 5)
    if min_stop_distance <= 0:
        min_stop_distance = 0.01
    min_stop_value = entry_price * (min_stop_distance / 100) if isinstance(min_stop_distance, float) else 0.01
    if max_stop_distance:
        max_stop_value = entry_price * (max_stop_distance / 100) if isinstance(max_stop_distance, float) else None
    
    price_change = (loss_amount_usd * leverage) / quantity
    safety_margin = min_stop_value * 1.5
    effective_price_change = max(price_change, safety_margin)
    
    if direction == "BUY":
        stop_loss = round(entry_price - effective_price_change, 5)
        final_stop = max(stop_loss, round(entry_price - min_stop_value * 2, 5))
        if max_stop_distance and final_stop < (entry_price - round(entry_price * (max_stop_distance / 100), 5)):
            final_stop = entry_price - round(entry_price * (max_stop_distance / 100), 5)
        return final_stop
    else:
        stop_loss = round(entry_price + effective_price_change, 5)
        final_stop = min(stop_loss, round(entry_price + min_stop_value * 2, 5))
        if max_stop_distance and final_stop > (entry_price + round(entry_price * (max_stop_distance / 100), 5)):
            final_stop = entry_price + round(entry_price * (max_stop_distance / 100), 5)
        return final_stop

def calculate_profit_loss_from_stop_loss(pos):
    entry_price = pos["entry_price"]
    stop_loss = pos["stop_loss"]
    quantity = pos["quantity"]
    leverage = 100.0
    if pos["direction"] == "BUY":
        profit_loss = (stop_loss - entry_price) * quantity / leverage
    else:
        profit_loss = (entry_price - stop_loss) * quantity / leverage
    return round(profit_loss, 2)

def calculate_current_profit(pos, current_bid, current_offer):
    """Calcula la ganancia/pérdida actual en USD."""
    entry_price = pos["entry_price"]
    quantity = pos["quantity"]
    leverage = 100.0  # Usaremos el leverage predeterminado hasta que obtengamos el real
    if pos["direction"] == "BUY":
        profit = (current_bid - entry_price) * quantity / leverage
    else:
        profit = (entry_price - current_offer) * quantity / leverage
    return round(profit, 2)

def convert_profit_to_usd(profit, symbol, current_bid):
    """Convierte la ganancia/pérdida a USD según el par de divisas."""
    if symbol == "USDMXN" and isinstance(profit, (int, float)):
        return round(profit / current_bid, 2)  # Convierte MXN a USD usando el bid actual
    return round(profit, 2)  # Asume USD por defecto

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    try:
        signal = Signal(**data)
        action, symbol, quantity, source, timeframe, loss_amount_usd = signal.action.lower(), signal.symbol, signal.quantity, signal.source, signal.timeframe, signal.loss_amount_usd
        last_signal_15m = load_signal()
        
        if timeframe == "15m":
            last_signal_15m[symbol] = action
            save_signal(last_signal_15m)
            return {"message": f"Última señal de 15m registrada para {symbol}: {action}"}
        
        global cst, x_security_token
        if not cst or not x_security_token:
            cst, x_security_token = authenticate()
        
        sync_open_positions(cst, x_security_token)
        
        min_size, current_bid, current_offer, spread, min_stop_distance, max_stop_distance = get_market_details(cst, x_security_token, symbol)
        adjusted_quantity = max(quantity, min_size)
        if adjusted_quantity != quantity:
            logger.info(f"Ajustando quantity de {quantity} a {adjusted_quantity} para cumplir con el tamaño mínimo")
        
        entry_price = current_bid if action == "buy" else current_offer
        entry_price = round(entry_price, 5)
        initial_stop_loss = calculate_valid_stop_loss(entry_price, action.upper(), loss_amount_usd, adjusted_quantity, 100.0, min_stop_distance, max_stop_distance)
        
        active_trades = get_active_trades(cst, x_security_token, symbol)
        if active_trades["buy"] > 0 or active_trades["sell"] > 0:
            if symbol in open_positions:
                pos = open_positions[symbol]
                opposite_action = "sell" if pos["direction"] == "BUY" else "buy"
                if action == opposite_action:
                    logger.info(f"Intentando cerrar posición para {symbol} con dealId: {pos['dealId']}")
                    profit_loss = 0.0
                    try:
                        deal_ref = close_position(cst, x_security_token, pos["dealId"], symbol, adjusted_quantity)
                        try:
                            confirmation = get_deal_confirmation(cst, x_security_token, deal_ref)
                            if "profit" in confirmation and confirmation["profit"] is not None:
                                profit_loss = float(confirmation["profit"])
                                profit_loss_usd = convert_profit_to_usd(profit_loss, symbol, current_bid)
                            else:
                                exit_price = float(confirmation.get("level", current_bid if pos["direction"] == "BUY" else current_offer))
                                quantity = pos["quantity"]
                                leverage = 100.0
                                if pos["direction"] == "BUY":
                                    profit_loss = (exit_price - pos["entry_price"]) * quantity / leverage
                                else:
                                    profit_loss = (pos["entry_price"] - exit_price) * quantity / leverage
                                profit_loss_usd = convert_profit_to_usd(profit_loss, symbol, current_bid)
                        except Exception as e:
                            logger.error(f"Error al obtener confirmación de cierre: {e}, usando precio actual como respaldo")
                            exit_price = current_bid if pos["direction"] == "BUY" else current_offer
                            quantity = pos["quantity"]
                            leverage = 100.0
                            if pos["direction"] == "BUY":
                                profit_loss = (exit_price - pos["entry_price"]) * quantity / leverage
                            else:
                                profit_loss = (pos["entry_price"] - exit_price) * quantity / leverage
                            profit_loss_usd = convert_profit_to_usd(profit_loss, symbol, current_bid)
                        
                        profit_loss_usd = round(profit_loss_usd, 2)
                        profit_loss_message = f"+${profit_loss_usd} USD" if profit_loss_usd >= 0 else f"-${abs(profit_loss_usd)} USD"
                        send_telegram_message(f"🔒 Posición cerrada para {symbol}: {pos['direction']} a {pos['entry_price']}. Ganancia/pérdida: {profit_loss_message}")
                        logger.info(f"Posición cerrada para {symbol} por señal opuesta, profit_loss: {profit_loss_usd} USD")
                    except Exception as e:
                        logger.error(f"Error al cerrar posición: {e}")
                        send_telegram_message(f"🔒 Posición cerrada para {symbol}: {pos['direction']} a {pos['entry_price']}. Ganancia/pérdida no calculada debido a error: {str(e)}")
                        raise HTTPException(status_code=500, detail=str(e))
                    finally:
                        if symbol in open_positions:
                            del open_positions[symbol]
                        
                        try:
                            new_active_trades = get_active_trades(cst, x_security_token, symbol)
                            if new_active_trades["buy"] == 0 and new_active_trades["sell"] == 0:
                                deal_ref = place_order(cst, x_security_token, action.upper(), symbol, adjusted_quantity, initial_stop_loss)
                                deal_id = get_position_deal_id(cst, x_security_token, symbol, action.upper())
                                logger.info(f"Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL {initial_stop_loss}, dealId: {deal_id}")
                                send_telegram_message(f"📈 Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL {initial_stop_loss} (dealId: {deal_id})")
                                open_positions[symbol] = {
                                    "direction": action.upper(),
                                    "entry_price": entry_price,
                                    "stop_loss": initial_stop_loss,
                                    "dealId": deal_id,
                                    "quantity": adjusted_quantity,
                                    "spread_at_open": spread
                                }
                                save_positions(open_positions)
                                return {"message": f"Posición cerrada y nueva orden {action.upper()} ejecutada para {symbol}"}
                            else:
                                raise Exception(f"No se pudo abrir la nueva orden: aún hay posiciones abiertas para {symbol}")
                        except Exception as e:
                            logger.error(f"Error al abrir nueva posición para {symbol}: {e}")
                            error_message = f"Posición cerrada, pero error al abrir nueva orden: {str(e)}"
                            send_telegram_message(f"❌ {error_message}")
                            return {"message": error_message}
            logger.info(f"Operación rechazada: Ya hay una operación abierta para {symbol}")
            send_telegram_message(f"⚠️ Operación rechazada para {symbol}: Ya hay una operación abierta")
            return {"message": f"Operación rechazada: Ya hay una operación abierta para {symbol}"}
        
        deal_ref = place_order(cst, x_security_token, action.upper(), symbol, adjusted_quantity, initial_stop_loss)
        deal_id = get_position_deal_id(cst, x_security_token, symbol, action.upper())
        logger.info(f"Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL {initial_stop_loss}, dealId: {deal_id}")
        send_telegram_message(f"📈 Orden {action.upper()} ejecutada para {symbol} a {entry_price} con SL {initial_stop_loss} (dealId: {deal_id})")
        
        open_positions[symbol] = {
            "direction": action.upper(),
            "entry_price": entry_price,
            "stop_loss": initial_stop_loss,
            "dealId": deal_id,
            "quantity": adjusted_quantity,
            "spread_at_open": spread
        }
        save_positions(open_positions)
        
        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        logger.error(f"Error en la ejecución: {e}")
        send_telegram_message(f"❌ Error en la ejecución: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

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
    payload = {"epic": epic, "direction": direction, "size": size, "type": "MARKET", "currencyCode": "USD"}
    if stop_loss is not None:
        if not isinstance(stop_loss, (int, float)) or stop_loss <= 0:
            logger.warning(f"Advertencia: stop_loss inválido ({stop_loss}), omitiendo stopLevel")
        else:
            payload["stopLevel"] = round(stop_loss, 5)
    
    try:
        response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload, timeout=10)
        if response.status_code != 200:
            error_msg = response.text
            logger.error(f"Error en place_order con stopLevel: {error_msg}")
            raise Exception(f"Error al ejecutar la orden: {error_msg}")
    except Exception as e:
        raise Exception(f"Error al ejecutar la orden: {str(e)}")
    
    response_json = response.json()
    deal_key = "dealReference" if "dealReference" in response_json else "dealId"
    if deal_key not in response_json:
        logger.error(f"Respuesta inesperada: {response_json}")
        raise Exception(f"No se encontró '{deal_key}' en la respuesta: {response_json}")
    return response_json[deal_key]

def close_position(cst: str, x_security_token: str, deal_id: str, epic: str, size: float):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    try:
        response = requests.delete(f"{CAPITAL_API_URL}/positions/{deal_id}", headers=headers, timeout=10)
        if response.status_code != 200:
            error_msg = response.text
            logger.error(f"Error en close_position: {error_msg}")
            raise Exception(f"Error al cerrar posición: {error_msg}")
        response_json = response.json()
        deal_ref = response_json.get("dealReference")
        return deal_ref
    except Exception as e:
        raise Exception(f"Error al cerrar posición: {str(e)}")

def update_stop_loss(cst: str, x_security_token: str, deal_id: str, new_stop_loss: float):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    payload = {"stopLevel": round(new_stop_loss, 5)}
    response = requests.put(f"{CAPITAL_API_URL}/positions/{deal_id}", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al actualizar stop loss: {response.text}")

async def monitor_trailing_stop():
    """Monitorea los precios y ajusta el stop loss cuando la ganancia es >= 3 USD."""
    global cst, x_security_token
    logger.setLevel(logging.INFO)
    logger.info("Iniciando monitoreo de trailing stop...")
    if not cst or not x_security_token:
        cst, x_security_token = authenticate()
    
    global open_positions
    open_positions = load_positions()
    if open_positions is None:
        open_positions = {}
    logger.info(f"Posiciones abiertas cargadas: {len(open_positions)} posiciones")
    
    while True:
        try:
            # Sincronizar posiciones periódicamente con la API
            sync_open_positions(cst, x_security_token)
            logger.info(f"Posiciones abiertas sincronizadas: {len(open_positions)} posiciones")
            
            if not open_positions:
                logger.info("No hay posiciones abiertas para monitorear")
                await asyncio.sleep(15)
                continue
            
            for symbol in list(open_positions.keys()):
                min_size, current_bid, current_offer, spread, min_stop_distance, max_stop_distance = get_market_details(cst, x_security_token, symbol)
                pos = open_positions[symbol]
                quantity = pos["quantity"]  # Usar el quantity real de la posición
                leverage = 100.0  # Placeholder; idealmente obtener de la API si disponible

                # Calcular la ganancia actual
                profit_usd = calculate_current_profit(pos, current_bid, current_offer)
                if symbol == "USDMXN":
                    profit_usd = convert_profit_to_usd(profit_usd, symbol, current_bid)

                logger.info(f"Monitoreando {symbol}: direction={pos['direction']}, entry_price={pos['entry_price']}, current_bid={current_bid}, current_offer={current_offer}, stop_loss={pos['stop_loss']}, profit_usd={profit_usd}, quantity={quantity}, leverage={leverage}")

                # Solo actualizar el stop loss si la ganancia es mayor a 3 USD
                if profit_usd < 3.0:
                    logger.info(f"No se actualiza stop loss para {symbol}: profit_usd={profit_usd} < 3.0 USD")
                    continue

                # Calcular el nuevo stop loss basado en el mínimo permitido por la API
                if pos["direction"] == "BUY":
                    new_stop_loss = round(current_bid - min_stop_distance, 5)
                    if new_stop_loss > pos["stop_loss"]:
                        update_stop_loss(cst, x_security_token, pos["dealId"], new_stop_loss)
                        pos["stop_loss"] = new_stop_loss
                        logger.info(f"Trailing stop actualizado para {symbol} (BUY): {new_stop_loss}, profit_usd={profit_usd}")
                        send_telegram_message(f"🔄 Trailing stop actualizado para {symbol} (BUY): {new_stop_loss}, profit: +${profit_usd} USD")
                    else:
                        logger.info(f"No se actualizó trailing stop para {symbol} (BUY): new_stop_loss={new_stop_loss} <= stop_loss={pos['stop_loss']}")
                else:  # SELL
                    new_stop_loss = round(current_offer + min_stop_distance, 5)
                    if new_stop_loss < pos["stop_loss"]:
                        update_stop_loss(cst, x_security_token, pos["dealId"], new_stop_loss)
                        pos["stop_loss"] = new_stop_loss
                        logger.info(f"Trailing stop actualizado para {symbol} (SELL): {new_stop_loss}, profit_usd={profit_usd}")
                        send_telegram_message(f"🔄 Trailing stop actualizado para {symbol} (SELL): {new_stop_loss}, profit: +${profit_usd} USD")
                    else:
                        logger.info(f"No se actualizó trailing stop para {symbol} (SELL): new_stop_loss={new_stop_loss} >= stop_loss={pos['stop_loss']}")
                
                save_positions(open_positions)
            await asyncio.sleep(15)
        except Exception as e:
            logger.error(f"Error en monitor_trailing_stop: {e}")
            send_telegram_message(f"❌ Error en monitoreo de trailing stop: {str(e)}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(monitor_trailing_stop())