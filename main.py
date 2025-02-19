from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests

app = FastAPI()

# Configuración de la API de Capital.com
CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = "39iCQ2YJgYEvhUOr"  # Reemplaza con tu API Key   
CUSTOM_PASSWORD = "MetEddRo1604*"  # Reemplaza con tu contraseña personalizada   
ACCOUNT_ID = "eddrd89@outlook.com"  # Reemplaza con tu Account ID   

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
        
        # Autenticación
        cst, x_security_token = authenticate()
        
        # Obtener posiciones abiertas
        open_positions = get_open_positions(cst, x_security_token)
        buy_count = sum(1 for pos in open_positions["positions"] if pos["position"]["direction"] == "BUY")
        sell_count = sum(1 for pos in open_positions["positions"] if pos["position"]["direction"] == "SELL")
        
        # Validar límite de operaciones
        if signal.action == "buy" and buy_count >= 2:
            return {"message": "Límite de 2 compras alcanzado. No se abrirá otra orden."}
        if signal.action == "sell" and sell_count >= 2:
            return {"message": "Límite de 2 ventas alcanzado. No se abrirá otra orden."}
        
        # Ejecutar la orden si no se ha alcanzado el límite
        place_order(cst, x_security_token, signal.action.upper(), signal.symbol, signal.quantity)
        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Función para autenticar en Capital.com
def authenticate():
    headers = {"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"}
    payload = {"identifier": ACCOUNT_ID, "password": CUSTOM_PASSWORD}
    response = requests.post(f"{CAPITAL_API_URL}/session", headers=headers, json=payload)
    
    if response.status_code != 200:
        raise Exception(f"Error de autenticación: {response.text}")
    
    return response.headers.get("CST"), response.headers.get("X-SECURITY-TOKEN")

# Función para obtener las posiciones abiertas
def get_open_positions(cst: str, x_security_token: str):
    headers = {"X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token}
    response = requests.get(f"{CAPITAL_API_URL}/positions", headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Error al obtener posiciones abiertas: {response.text}")
    
    return response.json()

# Función para ejecutar una orden en Capital.com
def place_order(cst: str, x_security_token: str, direction: str, epic: str, size: int = 10):
    MIN_SIZE = 100
    if size < MIN_SIZE:
        raise Exception(f"El tamaño mínimo de la orden es {MIN_SIZE}. Intentaste operar con {size}.")
    
    headers = {
        "X-CAP-API-KEY": API_KEY, "CST": cst, "X-SECURITY-TOKEN": x_security_token, "Content-Type": "application/json"
    }
    payload = {"epic": epic, "direction": direction, "size": size, "type": "MARKET", "currencyCode": "USD"}
    
    response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al ejecutar la orden: {response.text}")
    
    return response.json()