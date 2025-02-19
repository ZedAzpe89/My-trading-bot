from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests
import json

app = FastAPI()

# Configuración de la API de Capital.com
CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = "39iCQ2YJgYEvhUOr"  # Reemplaza con tu API Key   
CUSTOM_PASSWORD = "MetEddRo1604*"  # Reemplaza con tu contraseña personalizada   
ACCOUNT_ID = "eddrd89@outlook.com"  # Reemplaza con tu Account ID   

# Máximo de 2 compras y 2 ventas por símbolo
MAX_TRADES_PER_TYPE = 2

# Archivo para almacenar la última señal de 4H
SIGNAL_FILE = "last_signal_4h.json"

# Cargar la última señal de 4H desde un archivo
try:
    with open(SIGNAL_FILE, "r") as f:
        last_signal_4h = json.load(f)
except FileNotFoundError:
    last_signal_4h = {}

# Modelo para validar la entrada
class Signal(BaseModel):
    action: str
    symbol: str
    quantity: int = 1
    timeframe: str = "1m"  # Agregamos timeframe para diferenciar señales

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
        
        # Si la señal es de 4H, actualizar la última señal para el símbolo
        if timeframe == "4h":
            last_signal_4h[symbol] = action
            with open(SIGNAL_FILE, "w") as f:
                json.dump(last_signal_4h, f)
            print(f"Última señal de 4H para {symbol}: {action}")
            return {"message": f"Última señal de 4H registrada: {action}"}
        
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