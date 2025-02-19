from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests

app = FastAPI()

# Configuración de la API de Capital.com
CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = "39iCQ2YJgYEvhUOr"  # Reemplaza con tu API Key   
CUSTOM_PASSWORD = "MetEddRo1604*"  # Reemplaza con tu contraseña personalizada   
ACCOUNT_ID = "eddrd89@outlook.com"  # Reemplaza con tu Account ID   

# Diccionario para rastrear operaciones por símbolo
trade_limits = {}
MAX_TRADES_PER_TYPE = 2  # Máximo de 2 compras y 2 ventas por símbolo

# Modelo para validar la entrada
class Signal(BaseModel):
    action: str
    symbol: str
    quantity: int = 1

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
        
        # Verificar y actualizar el conteo de trades por símbolo
        if symbol not in trade_limits:
            trade_limits[symbol] = {"buy": 0, "sell": 0}
        
        if trade_limits[symbol][action] >= MAX_TRADES_PER_TYPE:
            return {"message": f"Límite de {MAX_TRADES_PER_TYPE} operaciones {action} alcanzado para {symbol}"}
        
        # Autenticar y obtener los tokens (CST y X-SECURITY-TOKEN)
        cst, x_security_token = authenticate()

        # Ejecutar la orden en Capital.com
        place_order(cst, x_security_token, action.upper(), symbol, quantity)
        trade_limits[symbol][action] += 1  # Aumentar el conteo de operaciones

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
    
    auth_data = response.json()
    print("Datos completos de autenticación:", auth_data)
    
    cst = response.headers.get("CST")
    x_security_token = response.headers.get("X-SECURITY-TOKEN")
    
    if not cst or not x_security_token:
        raise Exception("No se encontraron los tokens necesarios (CST, X-SECURITY-TOKEN).")
    
    return cst, x_security_token

# Función para ejecutar una orden en Capital.com
def place_order(cst: str, x_security_token: str, direction: str, epic: str, size: int = 10):
    MIN_SIZE = 100
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