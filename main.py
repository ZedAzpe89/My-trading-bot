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
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración de constantes y variables globales
CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = os.getenv("API_KEY")
CUSTOM_PASSWORD = os.getenv("CUSTOM_PASSWORD")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Las variables de entorno TELEGRAM_TOKEN y TELEGRAM_CHAT_ID deben estar definidas en Render.")

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

# Símbolos que operas
SYMBOLS_OPERATED = ["USDCAD", "EURUSD", "USDMXN"]

# Diccionario de distancias de stop loss fijas para 10 dólares de pérdida (source="volatility")
STOP_LOSS_DISTANCES = {
    "USDMXN": 0.02007,
    "USDCAD": 0.00143,
    "EURUSD": 0.00100,
    "USDJPY": 0.150
}

# Diccionario de distancias de stop loss fijas para 3 dólares de pérdida (source="no cons")
STOP_LOSS_DISTANCES_NO_CONS = {
    "USDMXN": 0.006024,
    "USDCAD": 0.000429,
    "EURUSD": 0.00030,
    "USDJPY": 0.045
}

# Diccionario para distancias de take profit (para 3 dólares de ganancia, source="no cons")
TAKE_PROFIT_DISTANCES_NO_CONS = {
    "USDMXN": 0.006024,
    "USDCAD": 0.000429,
    "EURUSD": 0.00030,
    "USDJPY": 0.045
}

# Definición de funciones auxiliares
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

def authenticate():
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
    # Ajustar min_stop_distance según el par de divisas
    min_stop_distance_raw = details["dealingRules"]["minStopOrProfitDistance"]["value"] if "minStopOrProfitDistance" in details["dealingRules"] else 10.0
    min_stop_distance_unit = details["dealingRules"]["minStopOrProfitDistance"]["unit"] if "minStopOrProfitDistance" in details["dealingRules"] else "POINTS"
    if min_stop_distance_unit == "POINTS":
        min_stop_distance = min_stop_distance_raw * 0.00001  # Convertir puntos a precio (5 decimales)
    else:  # PERCENTAGE
        min_stop_distance = current_bid * (min_stop_distance_raw / 100)
    min_stop_distance = max(min_stop_distance, 0.0001)  # Asegurar un mínimo razonable
    max_stop_distance = details["dealingRules"]["maxStopOrProfitDistance"]["value"] if "maxStopOrProfitDistance" in details["dealingRules"] else None
    logger.info(f"Detalles de mercado para {epic}: min_stop_distance={min_stop_distance}, unit={min_stop_distance_unit}")
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
                new_cst, new_x_security_token = authenticate()
                headers = {"X-CAP-API-KEY": API_KEY, "CST": new_cst, "X-SECURITY-TOKEN": new_x_security_token}
                response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
                if response.status_code != 200:
                    raise Exception(f"Error al sincronizar posiciones tras reautenticación: {response.text}")
                cst, x_security_token = new_cst, new_x_security_token
            else:
                raise Exception(f"Error al sincronizar posiciones: {response.text}")
        positions = response.json().get("positions", [])
        logger.info(f"Respuesta de la API para posiciones: {json.dumps(positions, indent=2)}")
        synced_positions = {}
        for pos in positions:
            epic = pos["market"]["epic"]
            try:
                stop_level = float(pos["position"].get("stopLevel", None)) if "stopLevel" in pos["position"] else None
            except (KeyError, TypeError):
                stop_level = None
                logger.warning(f"Advertencia: No se encontró stopLevel para posición en {epic}, usando None")
            size = float(pos["position"]["size"])
            # Ajustar quantity para que la distancia fija (sin spread) dé 10 dólares (o 3 dólares para "no cons")
            if epic == "USDCAD":
                quantity = 699300.7
            elif epic == "EURUSD":
                quantity = 1000000.0
            elif epic == "USDMXN":
                quantity = 49801.0
            elif epic == "USDJPY":
                quantity = 6666.67
            else:
                quantity = size * 100000
            synced_positions[epic] = {
                "direction": pos["position"]["direction"],
                "entry_price": float(pos["position"]["level"]),
                "stop_loss": stop_level,
                "dealId": pos["position"]["dealId"],
                "quantity": quantity,
                "upl": float(pos["position"]["upl"]) if "upl" in pos["position"] else 0.0,
                "source": open_positions.get(epic, {}).get("source", "volatility"),
                "spread_at_open": open_positions.get(epic, {}).get("spread_at_open", 0.0),
                "take_profit": open_positions.get(epic, {}).get("take_profit", None),
                "highest_price": float(pos["position"]["level"]),  # Para trailing stop
                "lowest_price": float(pos["position"]["level"]),   # Para trailing stop
                "trailing_active": open_positions.get(epic, {}).get("trailing_active", False)
            }
            logger.info(f"Sincronizando {epic}: size={size}, quantity={quantity} (ajustado), upl={synced_positions[epic]['upl']}")
        
        closed_positions = {k: v for k, v in open_positions.items() if k not in synced_positions}
        for symbol, pos in closed_positions.items():
            if pos["stop_loss"] and (pos["direction"] == "BUY" and pos["stop_loss"] >= pos["entry_price"]) or (pos["direction"] == "SELL" and pos["stop_loss"] <= pos["entry_price"]):
                profit_loss = calculate_profit_loss_from_stop_loss(pos)
                profit_loss_message = f"+${profit_loss} USD" if profit_loss >= 0 else f"-${abs(profit_loss)} USD"
                send_telegram_message(f"🔒 Posición cerrada por stop loss para {symbol}: {pos['direction']} a {pos['entry_price']}. Ganancia/pérdida: {profit_loss_message}")
                logger.info(f"Posición cerrada por stop loss para {symbol}, profit_loss: {profit_loss} USD")
        
        open_positions = synced_positions
        save_positions(open_positions)
        return cst, x_security_token
    except Exception as e:
        logger.error(f"Error en sync_open_positions: {e}")
        raise

def calculate_valid_stop_loss(entry_price, direction, loss_amount_usd, quantity, leverage, min_stop_distance, max_stop_distance=None, symbol=None, spread=None, source=None, current_bid=None, current_offer=None):
    entry_price = round(entry_price, 5)
    if symbol not in STOP_LOSS_DISTANCES:
        raise ValueError(f"Símbolo {symbol} no soportado")
    
    # Seleccionar la distancia fija según el source
    if source == "no cons":
        fixed_stop_distance = STOP_LOSS_DISTANCES_NO_CONS[symbol]
    else:  # source="volatility"
        fixed_stop_distance = STOP_LOSS_DISTANCES[symbol]
    
    # Ajustar la distancia restando el spread para que la pérdida neta sea exacta
    adjusted_stop_distance = fixed_stop_distance - spread
    adjusted_stop_distance = max(adjusted_stop_distance, 0.00001)
    logger.info(f"Cálculo de stop loss para {symbol}: entry_price={entry_price}, fixed_stop_distance={fixed_stop_distance}, spread={spread}, adjusted_stop_distance={adjusted_stop_distance}, direction={direction}, source={source}")
    
    if direction == "BUY":
        stop_loss = entry_price - adjusted_stop_distance
        # Verificar que el stop loss cumpla con min_stop_distance
        min_allowed_stop_loss = current_bid - min_stop_distance
        if stop_loss > min_allowed_stop_loss:
            stop_loss = min_allowed_stop_loss
            new_loss_amount = abs((stop_loss - entry_price) * quantity / leverage)
            logger.warning(f"Stop loss ajustado para cumplir con min_stop_distance: {stop_loss}, nueva pérdida inicial: {new_loss_amount} USD")
            send_telegram_message(f"⚠️ Stop loss ajustado para {symbol} (BUY) a {stop_loss} para cumplir con las restricciones del bróker. Pérdida inicial: -${new_loss_amount} USD")
    else:  # SELL
        stop_loss = entry_price + adjusted_stop_distance
        # Verificar que el stop loss cumpla con min_stop_distance
        max_allowed_stop_loss = current_offer + min_stop_distance
        if stop_loss < max_allowed_stop_loss:
            stop_loss = max_allowed_stop_loss
            new_loss_amount = abs((stop_loss - entry_price) * quantity / leverage)
            logger.warning(f"Stop loss ajustado para cumplir con min_stop_distance: {stop_loss}, nueva pérdida inicial: {new_loss_amount} USD")
            send_telegram_message(f"⚠️ Stop loss ajustado para {symbol} (SELL) a {stop_loss} para cumplir con las restricciones del bróker. Pérdida inicial: -${new_loss_amount} USD")
    
    return round(stop_loss, 5)

def calculate_take_profit(entry_price, direction, profit_amount_usd, quantity, leverage, symbol, source):
    if source != "no cons":
        return None  # Solo aplicamos take profit para source="no cons"
    
    if symbol not in TAKE_PROFIT_DISTANCES_NO_CONS:
        raise ValueError(f"Símbolo {symbol} no soportado para take profit")
    
    take_profit_distance = TAKE_PROFIT_DISTANCES_NO_CONS[symbol]
    if direction == "BUY":
        take_profit = entry_price + take_profit_distance
    else:  # SELL
        take_profit = entry_price - take_profit_distance
    
    return round(take_profit, 5)

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
    entry_price = pos["entry_price"]
    quantity = pos["quantity"]
    leverage = 100.0
    if pos["direction"] == "BUY":
        profit = (current_bid - entry_price) * quantity / leverage
    else:
        profit = (entry_price - current_offer) * quantity / leverage
    logger.info(f"Cálculo de profit para {pos['direction']} {entry_price} -> {current_bid if pos['direction'] == 'BUY' else current_offer}: profit={profit} USD, quantity={quantity}, leverage={leverage}")
    return profit

def convert_profit_to_usd(profit, symbol, current_bid):
    return round(profit, 2)

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
            payload["stopLevel"] = stop_loss
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

def update_stop_loss(cst: str, x_security_token: str, deal_id: str, new_stop_loss: float, symbol: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"}
    new_stop_loss = round(new_stop_loss, 5)  # Todos los pares usan 5 decimales
    payload = {"stopLevel": new_stop_loss}
    response = requests.put(f"{CAPITAL_API_URL}/positions/{deal_id}", headers=headers, json=payload)
    if response.status_code != 200:
        error_msg = response.json() if response.text else "Respuesta vacía"
        logger.error(f"Error al actualizar stop loss: {error_msg}")
        raise Exception(f"Error al actualizar stop loss: {error_msg}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global open_positions, cst, x_security_token
    logger.setLevel(logging.INFO)
    open_positions = load_positions()
    cst, x_security_token = authenticate()
    cst, x_security_token = sync_open_positions(cst, x_security_token)
    
    # Sincronizar estados de consolidación al iniciar
    last_signal_15m = load_signal()
    # Inicializar estados para los símbolos operados si no están presentes
    for symbol in SYMBOLS_OPERATED:
        if symbol not in last_signal_15m:
            last_signal_15m[symbol] = "Fin Consolidación"  # Estado por defecto
    save_signal(last_signal_15m)
    logger.info(f"Estados de consolidación sincronizados al inicio: {last_signal_15m}")
    
    logger.info("🚀 Bot iniciado correctamente.")
    yield
    logger.info("Cerrando aplicación...")

app = FastAPI(lifespan=lifespan)

class Signal(BaseModel):
    action: str
    symbol: str
    quantity: float = 10000.0
    source: str = "rsi"
    timeframe: str = "1m"
    loss_amount_usd: float = 10.0

@app.post("/webhook")
async def webhook(request: Request):
    global open_positions, cst, x_security_token
    if cst is None or x_security_token is None:
        cst, x_security_token = authenticate()
    
    data = await request.json()
    try:
        signal = Signal(**data)
        action, symbol, quantity, source, timeframe, loss_amount_usd = signal.action.lower(), signal.symbol, signal.quantity, signal.source, signal.timeframe, signal.loss_amount_usd
        last_signal_15m = load_signal()
        
        # Actualizar estado de consolidación si la señal es de 15m
        if timeframe == "15m":
            if "inicio" in action.lower():
                last_signal_15m[symbol] = "Inicio Consolidación"
            elif "fin" in action.lower():
                last_signal_15m[symbol] = "Fin Consolidación"
            save_signal(last_signal_15m)
            logger.info(f"Estado de consolidación actualizado para {symbol}: {last_signal_15m[symbol]}")
            return {"message": f"Última señal de 15m registrada para {symbol}: {last_signal_15m[symbol]}"}
        
        # Verificar el estado de consolidación antes de operar
        market_state = last_signal_15m.get(symbol, "Fin Consolidación")
        if market_state == "Inicio Consolidación" and source != "no cons":
            rejection_message = (
                f"⚠️ Operación rechazada para {symbol}: El mercado está en un rango de consolidación. "
                "Se recomienda esperar a que el precio salga del rango."
            )
            logger.info(rejection_message)
            send_telegram_message(rejection_message)
            return {"message": rejection_message}
        
        cst, x_security_token = sync_open_positions(cst, x_security_token)
        
        min_size, current_bid, current_offer, spread, min_stop_distance, max_stop_distance = get_market_details(cst, x_security_token, symbol)
        adjusted_quantity = max(quantity, min_size)
        if adjusted_quantity != quantity:
            logger.info(f"Ajustando quantity de {quantity} a {adjusted_quantity} para cumplir con el tamaño mínimo")
        
        entry_price = current_bid if action == "buy" else current_offer
        entry_price = round(entry_price, 5)
        initial_stop_loss = calculate_valid_stop_loss(
            entry_price=entry_price,
            direction=action.upper(),
            loss_amount_usd=loss_amount_usd,
            quantity=adjusted_quantity,
            leverage=100.0,
            min_stop_distance=min_stop_distance,
            max_stop_distance=max_stop_distance,
            symbol=symbol,
            spread=spread,
            source=source,
            current_bid=current_bid,
            current_offer=current_offer
        )
        take_profit = calculate_take_profit(entry_price, action.upper(), 3.0, adjusted_quantity, 100.0, symbol, source)
        logger.info(f"Initial stop loss calculado para {symbol}: entry_price={entry_price}, initial_stop_loss={initial_stop_loss}, take_profit={take_profit}")
        
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
                            else:
                                exit_price = float(confirmation.get("level", current_bid if pos["direction"] == "BUY" else current_offer))
                                quantity = pos["quantity"]
                                leverage = 100.0
                                if pos["direction"] == "BUY":
                                    profit_loss = (exit_price - pos["entry_price"]) * quantity / leverage
                                else:
                                    profit_loss = (pos["entry_price"] - exit_price) * quantity / leverage
                        except Exception as e:
                            logger.error(f"Error al obtener confirmación de cierre: {e}, usando precio actual como respaldo")
                            exit_price = current_bid if pos["direction"] == "BUY" else current_offer
                            quantity = pos["quantity"]
                            leverage = 100.0
                            if pos["direction"] == "BUY":
                                profit_loss = (exit_price - pos["entry_price"]) * quantity / leverage
                            else:
                                profit_loss = (pos["entry_price"] - exit_price) * quantity / leverage
                        
                        profit_loss = round(profit_loss, 2)
                        profit_loss_message = f"+${profit_loss} USD" if profit_loss >= 0 else f"-${abs(profit_loss)} USD"
                        send_telegram_message(f"🔒 Posición cerrada para {symbol}: {pos['direction']} a {pos['entry_price']}. Ganancia/pérdida: {profit_loss_message}")
                        logger.info(f"Posición cerrada para {symbol} por señal opuesta, profit_loss: {profit_loss} USD")
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
                                    "spread_at_open": spread,
                                    "source": source,
                                    "take_profit": take_profit,
                                    "highest_price": entry_price,
                                    "lowest_price": entry_price,
                                    "trailing_active": False
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
            "spread_at_open": spread,
            "source": source,
            "take_profit": take_profit,
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "trailing_active": False
        }
        save_positions(open_positions)
        
        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        logger.error(f"Error en la ejecución: {e}")
        send_telegram_message(f"❌ Error en la ejecución: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def monitor_trailing_stop():
    global open_positions
    cst, x_security_token = authenticate()
    logger.setLevel(logging.INFO)
    logger.info("Iniciando monitoreo de trailing stop...")
    
    open_positions = load_positions()
    if open_positions is None:
        open_positions = {}
    logger.info(f"Posiciones abiertas cargadas: {len(open_positions)} posiciones")
    
    while True:
        try:
            cst, x_security_token = sync_open_positions(cst, x_security_token)
            logger.info(f"Posiciones abiertas sincronizadas: {len(open_positions)} posiciones")
            
            if not open_positions:
                logger.info("No hay posiciones abiertas para monitorear")
                await asyncio.sleep(15)
                continue
            
            for symbol in list(open_positions.keys()):
                min_size, current_bid, current_offer, spread, min_stop_distance, max_stop_distance = get_market_details(cst, x_security_token, symbol)
                pos = open_positions[symbol]
                quantity = pos["quantity"]
                leverage = 100.0
                upl = pos["upl"]  # Usar el valor de upl de la sincronización

                # Calcular profit manualmente para depuración
                calculated_profit = calculate_current_profit(pos, current_bid, current_offer)
                logger.info(f"Comparación para {symbol}: upl={upl} USD (de API), calculated_profit={calculated_profit} USD (manual)")

                # Usar upl como profit_usd
                profit_usd = upl

                # Actualizar precios máximo y mínimo alcanzados
                pos["highest_price"] = max(pos["highest_price"], current_bid if pos["direction"] == "BUY" else current_offer)
                pos["lowest_price"] = min(pos["lowest_price"], current_bid if pos["direction"] == "BUY" else current_offer)

                # Todos los pares (USDCAD, USDMXN, EURUSD) usan 5 decimales
                pip_value = 0.00001
                decimal_places = 5

                current_bid = round(current_bid, decimal_places)
                current_offer = round(current_offer, decimal_places)

                logger.info(f"Monitoreando {symbol}: direction={pos['direction']}, entry_price={pos['entry_price']}, current_bid={current_bid}, current_offer={current_offer}, stop_loss={pos['stop_loss']}, profit_usd={profit_usd}, quantity={quantity}, leverage={leverage}, min_stop_distance={min_stop_distance}, stop_loss_for_0_usd={pos['entry_price']}")

                # Lógica para source="volatility"
                if pos["source"] == "volatility":
                    # Mover stop loss a 0 dólares de pérdida cuando la ganancia alcance 10 dólares
                    if profit_usd >= 10.0 and pos["stop_loss"] != pos["entry_price"]:
                        new_stop_loss = pos["entry_price"]
                        if pos["direction"] == "BUY":
                            max_allowed_stop_loss = current_bid - min_stop_distance
                            new_stop_loss = min(new_stop_loss, max_allowed_stop_loss)
                        else:  # SELL
                            min_allowed_stop_loss = current_offer + min_stop_distance
                            new_stop_loss = max(new_stop_loss, min_allowed_stop_loss)
                        new_stop_loss = round(new_stop_loss, decimal_places)
                        if (pos["direction"] == "BUY" and new_stop_loss > pos["stop_loss"]) or (pos["direction"] == "SELL" and new_stop_loss < pos["stop_loss"]):
                            try:
                                update_stop_loss(cst, x_security_token, pos["dealId"], new_stop_loss, symbol)
                                pos["stop_loss"] = new_stop_loss
                                logger.info(f"Stop loss ajustado a 0 dólares de pérdida para {symbol}: {new_stop_loss}, profit_usd={profit_usd}")
                                send_telegram_message(f"🔄 Stop loss ajustado a 0 dólares de pérdida para {symbol}: {new_stop_loss}, profit: +${profit_usd} USD")
                            except Exception as e:
                                logger.error(f"Error al actualizar stop loss: {e}")
                                send_telegram_message(f"❌ Error al actualizar stop loss para {symbol}: {str(e)}")

                    # Activar trailing stop loss a 3 dólares de distancia cuando la ganancia alcance 13 dólares
                    if profit_usd >= 13.0:
                        pos["trailing_active"] = True

                    if pos["trailing_active"]:
                        trailing_distance = (3.0 * leverage) / quantity  # Distancia para 3 dólares
                        if pos["direction"] == "BUY":
                            new_stop_loss = pos["highest_price"] - trailing_distance
                            max_allowed_stop_loss = current_bid - min_stop_distance
                            new_stop_loss = min(new_stop_loss, max_allowed_stop_loss)
                            new_stop_loss = round(new_stop_loss, decimal_places)
                            if new_stop_loss > pos["stop_loss"]:
                                try:
                                    update_stop_loss(cst, x_security_token, pos["dealId"], new_stop_loss, symbol)
                                    pos["stop_loss"] = new_stop_loss
                                    logger.info(f"Trailing stop actualizado para {symbol} (BUY): {new_stop_loss}, profit_usd={profit_usd}")
                                    send_telegram_message(f"🔄 Trailing stop actualizado para {symbol} (BUY): {new_stop_loss}, profit: +${profit_usd} USD")
                                except Exception as e:
                                    if "error.invalid.stoploss.maxvalue" in str(e):
                                        error_msg = str(e)
                                        max_allowed_value = float(error_msg.split(": ")[-1].strip("}"))
                                        adjusted_min_stop_distance = current_bid - max_allowed_value
                                        logger.warning(f"Ajustando min_stop_distance a {adjusted_min_stop_distance} basado en el error: {e}")
                                        max_allowed_stop_loss = max_allowed_value
                                        new_stop_loss = min(new_stop_loss, max_allowed_stop_loss)
                                        new_stop_loss = round(new_stop_loss, decimal_places)
                                        update_stop_loss(cst, x_security_token, pos["dealId"], new_stop_loss, symbol)
                                        pos["stop_loss"] = new_stop_loss
                                        logger.info(f"Trailing stop actualizado con ajuste para {symbol} (BUY): {new_stop_loss}, profit_usd={profit_usd}")
                                        send_telegram_message(f"🔄 Trailing stop actualizado con ajuste para {symbol} (BUY): {new_stop_loss}, profit: +${profit_usd} USD")
                                    else:
                                        logger.error(f"Error al actualizar stop loss: {e}")
                                        send_telegram_message(f"❌ Error al actualizar stop loss para {symbol}: {str(e)}")
                        else:  # SELL
                            new_stop_loss = pos["lowest_price"] + trailing_distance
                            min_allowed_stop_loss = current_offer + min_stop_distance
                            new_stop_loss = max(new_stop_loss, min_allowed_stop_loss)
                            new_stop_loss = round(new_stop_loss, decimal_places)
                            if new_stop_loss < pos["stop_loss"]:
                                try:
                                    update_stop_loss(cst, x_security_token, pos["dealId"], new_stop_loss, symbol)
                                    pos["stop_loss"] = new_stop_loss
                                    logger.info(f"Trailing stop actualizado para {symbol} (SELL): {new_stop_loss}, profit_usd={profit_usd}")
                                    send_telegram_message(f"🔄 Trailing stop actualizado para {symbol} (SELL): {new_stop_loss}, profit: +${profit_usd} USD")
                                except Exception as e:
                                    if "error.invalid.stoploss.minvalue" in str(e):
                                        error_msg = str(e)
                                        min_allowed_value = float(error_msg.split(": ")[-1].strip("}"))
                                        adjusted_min_stop_distance = min_allowed_value - current_offer
                                        logger.warning(f"Ajustando min_stop_distance a {adjusted_min_stop_distance} basado en el error: {e}")
                                        min_allowed_stop_loss = min_allowed_value
                                        new_stop_loss = max(new_stop_loss, min_allowed_stop_loss)
                                        new_stop_loss = round(new_stop_loss, decimal_places)
                                        update_stop_loss(cst, x_security_token, pos["dealId"], new_stop_loss, symbol)
                                        pos["stop_loss"] = new_stop_loss
                                        logger.info(f"Trailing stop actualizado con ajuste para {symbol} (SELL): {new_stop_loss}, profit_usd={profit_usd}")
                                        send_telegram_message(f"🔄 Trailing stop actualizado con ajuste para {symbol} (SELL): {new_stop_loss}, profit: +${profit_usd} USD")
                                    else:
                                        logger.error(f"Error al actualizar stop loss: {e}")
                                        send_telegram_message(f"❌ Error al actualizar stop loss para {symbol}: {str(e)}")
                    else:
                        logger.info(f"No se actualiza trailing stop para {symbol}: profit_usd={profit_usd} < 13.0 USD o trailing no activo")

                # Lógica para source="no cons"
                if pos["source"] == "no cons" and pos["take_profit"]:
                    current_price = current_bid if pos["direction"] == "BUY" else current_offer
                    if pos["direction"] == "BUY" and current_price >= pos["take_profit"]:
                        deal_ref = close_position(cst, x_security_token, pos["dealId"], symbol, pos["quantity"])
                        profit_loss = 3.0  # Ganancia objetivo
                        profit_loss_message = f"+${profit_loss} USD"
                        send_telegram_message(f"🔒 Posición cerrada por take profit para {symbol}: {pos['direction']} a {pos['entry_price']}. Ganancia: {profit_loss_message}")
                        logger.info(f"Posición cerrada por take profit para {symbol}, profit_loss: {profit_loss} USD")
                        del open_positions[symbol]
                    elif pos["direction"] == "SELL" and current_price <= pos["take_profit"]:
                        deal_ref = close_position(cst, x_security_token, pos["dealId"], symbol, pos["quantity"])
                        profit_loss = 3.0  # Ganancia objetivo
                        profit_loss_message = f"+${profit_loss} USD"
                        send_telegram_message(f"🔒 Posición cerrada por take profit para {symbol}: {pos['direction']} a {pos['entry_price']}. Ganancia: {profit_loss_message}")
                        logger.info(f"Posición cerrada por take profit para {symbol}, profit_loss: {profit_loss} USD")
                        del open_positions[symbol]
                
                save_positions(open_positions)
            await asyncio.sleep(15)
        except Exception as e:
            logger.error(f"Error en monitor_trailing_stop: {e}")
            send_telegram_message(f"❌ Error en monitoreo de trailing stop: {str(e)}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(monitor_trailing_stop())