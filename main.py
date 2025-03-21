import requests
import json
import os
import logging
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Dict, Optional
import time

app = FastAPI()

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables globales
API_KEY = os.getenv("CAPITAL_API_KEY", "your_api_key")
CAPITAL_API_URL = "https://api-capital.backend-capital.com/api/v1"
open_positions: Dict[str, dict] = {}
cst: Optional[str] = None
x_security_token: Optional[str] = None

# Diccionario de distancias de stop loss fijas (para 10 dólares de pérdida, source="volatility")
STOP_LOSS_DISTANCES = {
    "USDMXN": 0.02007,
    "USDCAD": 0.00143,
    "EURUSD": 0.00100,
    "USDJPY": 0.150
}

# Diccionario de distancias de stop loss fijas (para 3 dólares de pérdida, source="no cons")
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

# Modelo de datos para la señal
class Signal(BaseModel):
    action: str
    symbol: str
    quantity: float
    source: str
    timeframe: str
    loss_amount_usd: float

# Funciones auxiliares
def authenticate():
    headers = {"X-CAP-API-KEY": API_KEY}
    payload = {
        "identifier": os.getenv("CAPITAL_EMAIL", "your_email"),
        "password": os.getenv("CAPITAL_PASSWORD", "your_password")
    }
    response = requests.post(f"{CAPITAL_API_URL}/session", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al autenticar: {response.text}")
    response_headers = response.headers
    return response_headers["CST"], response_headers["X-SECURITY-TOKEN"]

def save_positions(positions: Dict[str, dict]):
    with open("positions.json", "w") as f:
        json.dump(positions, f)

def load_positions() -> Dict[str, dict]:
    try:
        with open("positions.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_signal(signal: Dict[str, str]):
    with open("last_signal_15m.json", "w") as f:
        json.dump(signal, f)

def load_signal() -> Dict[str, str]:
    try:
        with open("last_signal_15m.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def get_market_details(cst: str, x_security_token: str, symbol: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/markets?search={symbol}", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener detalles del mercado: {response.text}")
    markets = response.json().get("markets", [])
    if not markets:
        raise Exception(f"No se encontró el mercado para {symbol}")
    market = markets[0]
    min_size = float(market["dealingRules"]["minDealSize"]["value"])
    response = requests.get(f"{CAPITAL_API_URL}/prices?epic={market['epic']}&resolution=MINUTE", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener precios: {response.text}")
    prices = response.json().get("prices", [])
    if not prices:
        raise Exception(f"No se encontraron precios para {symbol}")
    current_bid = float(prices[-1]["closePrice"]["bid"])
    current_offer = float(prices[-1]["closePrice"]["ask"])
    spread = current_offer - current_bid
    min_stop_distance = float(market["dealingRules"]["minStopDistance"]["value"])
    max_stop_distance = float(market["dealingRules"]["maxStopDistance"]["value"]) if "maxStopDistance" in market["dealingRules"] else None
    logger.info(f"Detalles de mercado para {symbol}: min_stop_distance={min_stop_distance}, unit={market['dealingRules']['minStopDistance']['unit']}")
    return min_size, current_bid, current_offer, spread, min_stop_distance, max_stop_distance

def calculate_valid_stop_loss(entry_price, direction, loss_amount_usd, quantity, leverage, min_stop_distance, max_stop_distance=None, symbol=None, spread=None, source=None):
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
    else:  # SELL
        stop_loss = entry_price + adjusted_stop_distance
    
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

def calculate_profit_loss_from_stop_loss(position: dict) -> float:
    entry_price = position["entry_price"]
    stop_loss = position["stop_loss"]
    quantity = position["quantity"]
    leverage = 100.0
    if position["direction"] == "BUY":
        return (stop_loss - entry_price) * quantity / leverage
    else:
        return (entry_price - stop_loss) * quantity / leverage

def get_active_trades(cst: str, x_security_token: str, symbol: str) -> Dict[str, int]:
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener posiciones: {response.text}")
    positions = response.json().get("positions", [])
    active_trades = {"buy": 0, "sell": 0}
    for pos in positions:
        if pos["market"]["epic"] == symbol:
            direction = pos["position"]["direction"].lower()
            active_trades[direction] += 1
    return active_trades

def place_order(cst: str, x_security_token: str, direction: str, symbol: str, quantity: float, stop_loss: float):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    payload = {
        "direction": direction,
        "epic": symbol,
        "size": quantity / 100000,
        "stopLevel": stop_loss,
        "guaranteedStop": False
    }
    response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al colocar orden: {response.text}")
    return response.json().get("dealReference")

def close_position(cst: str, x_security_token: str, deal_id: str, symbol: str, quantity: float):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    payload = {
        "dealId": deal_id,
        "direction": "SELL" if open_positions[symbol]["direction"] == "BUY" else "BUY",
        "size": quantity / 100000,
        "orderType": "MARKET"
    }
    response = requests.delete(f"{CAPITAL_API_URL}/positions/{deal_id}", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al cerrar posición: {response.text}")
    return response.json().get("dealReference")

def get_deal_confirmation(cst: str, x_security_token: str, deal_ref: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/confirms/{deal_ref}", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener confirmación: {response.text}")
    return response.json()

def get_position_deal_id(cst: str, x_security_token: str, symbol: str, direction: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    if response.status_code != 200:
        raise Exception(f"Error al obtener posiciones: {response.text}")
    positions = response.json().get("positions", [])
    for pos in positions:
        if pos["market"]["epic"] == symbol and pos["position"]["direction"] == direction:
            return pos["position"]["dealId"]
    raise Exception(f"No se encontró la posición para {symbol} con dirección {direction}")

def send_telegram_message(message: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "your_bot_token")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "your_chat_id")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    requests.post(url, json=payload)

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
                "trailing_active": False
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

def update_position(cst: str, x_security_token: str, symbol: str):
    if symbol not in open_positions:
        return
    
    pos = open_positions[symbol]
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    
    # Obtener precios actuales
    _, current_bid, current_offer, _, _, _ = get_market_details(cst, x_security_token, symbol)
    current_price = current_bid if pos["direction"] == "BUY" else current_offer
    
    # Actualizar precios máximo y mínimo alcanzados
    pos["highest_price"] = max(pos["highest_price"], current_price)
    pos["lowest_price"] = min(pos["lowest_price"], current_price)
    
    # Calcular ganancia/pérdida actual
    if pos["direction"] == "BUY":
        profit_loss = (current_price - pos["entry_price"]) * pos["quantity"] / 100.0
    else:
        profit_loss = (pos["entry_price"] - current_price) * pos["quantity"] / 100.0
    
    # Lógica para source="volatility"
    if pos["source"] == "volatility":
        # Mover stop loss a 0 dólares de pérdida cuando la ganancia alcance 10 dólares
        if profit_loss >= 10.0 and pos["stop_loss"] != pos["entry_price"]:
            new_stop_loss = pos["entry_price"]
            payload = {
                "stopLevel": new_stop_loss,
                "guaranteedStop": False
            }
            response = requests.put(f"{CAPITAL_API_URL}/positions/{pos['dealId']}", headers=headers, json=payload)
            if response.status_code == 200:
                pos["stop_loss"] = new_stop_loss
                logger.info(f"Stop loss ajustado a 0 dólares de pérdida para {symbol}: {new_stop_loss}")
                send_telegram_message(f"🔄 Stop loss ajustado a 0 dólares de pérdida para {symbol} a {new_stop_loss}")
        
        # Activar trailing stop loss a 3 dólares de distancia cuando la ganancia alcance 13 dólares
        if profit_loss >= 13.0:
            pos["trailing_active"] = True
        
        if pos["trailing_active"]:
            trailing_distance = (3.0 * 100.0) / pos["quantity"]  # Distancia para 3 dólares
            if pos["direction"] == "BUY":
                new_stop_loss = pos["highest_price"] - trailing_distance
                if new_stop_loss > pos["stop_loss"]:
                    payload = {
                        "stopLevel": round(new_stop_loss, 5),
                        "guaranteedStop": False
                    }
                    response = requests.put(f"{CAPITAL_API_URL}/positions/{pos['dealId']}", headers=headers, json=payload)
                    if response.status_code == 200:
                        pos["stop_loss"] = new_stop_loss
                        logger.info(f"Trailing stop loss ajustado para {symbol}: {new_stop_loss}")
                        send_telegram_message(f"🔄 Trailing stop loss ajustado para {symbol} a {new_stop_loss}")
            else:  # SELL
                new_stop_loss = pos["lowest_price"] + trailing_distance
                if new_stop_loss < pos["stop_loss"]:
                    payload = {
                        "stopLevel": round(new_stop_loss, 5),
                        "guaranteedStop": False
                    }
                    response = requests.put(f"{CAPITAL_API_URL}/positions/{pos['dealId']}", headers=headers, json=payload)
                    if response.status_code == 200:
                        pos["stop_loss"] = new_stop_loss
                        logger.info(f"Trailing stop loss ajustado para {symbol}: {new_stop_loss}")
                        send_telegram_message(f"🔄 Trailing stop loss ajustado para {symbol} a {new_stop_loss}")
    
    # Lógica para source="no cons"
    if pos["source"] == "no cons" and pos["take_profit"]:
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
        initial_stop_loss = calculate_valid_stop_loss(entry_price, action.upper(), loss_amount_usd, adjusted_quantity, 100.0, min_stop_distance, max_stop_distance, symbol, spread, source)
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

@app.on_event("startup")
async def startup_event():
    global open_positions, cst, x_security_token
    open_positions = load_positions()
    cst, x_security_token = authenticate()
    while True:
        try:
            cst, x_security_token = sync_open_positions(cst, x_security_token)
            for symbol in list(open_positions.keys()):
                update_position(cst, x_security_token, symbol)
        except Exception as e:
            logger.error(f"Error en el bucle de actualización: {e}")
        time.sleep(60)  # Actualizar cada 60 segundos