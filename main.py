from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests

app = FastAPI()

# Configuración de la API de Capital.com
CAPITAL_API_URL = "https://demo-api-capital.backend-capital.com/api/v1"
API_KEY = "sdfrg34YEvhUOr"  # Reemplaza con tu API Key  
CUSTOM_PASSWORD = "Mewerw4fgg4"  # Reemplaza con tu contraseña personalizada  
ACCOUNT_ID = "edw4439@outlook.com"  # Reemplaza con tu Account ID  

# Modelo para validar la entrada
class Signal(BaseModel):
    action: str
    symbol: str
    quantity: int = 1

# Endpoint para recibir alertas de TradingView
@app.post("/webhook")
async def webhook(signal: Signal):

    try:
        # Procesar la señal de TradingView
        action = signal.action
        symbol = signal.symbol
        quantity = signal.quantity

        # Autenticar y obtener el token
        token = authenticate()

        # Ejecutar la orden en Capital.com
        if action == "buy":
            place_order(token, "BUY", symbol, quantity)
        elif action == "sell":
            place_order(token, "SELL", symbol, quantity)
        else:
            raise HTTPException(status_code=400, detail="Acción no válida")

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
    if response.status_code != 200:
        raise Exception(f"Error de autenticación: {response.text}")
    
    # Obtener el token de la respuesta
    auth_data = response.json()
    return auth_data["token"]

# Función para ejecutar una orden en Capital.com
def place_order(token: str, direction: str, epic: str, size: int):
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "type": "MARKET",  # Tipo de orden (MARKET, LIMIT, etc.)
        "currencyCode": "USD"  # Moneda de la operación
    }
    # Cambiar la URL a /positions
    response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al ejecutar la orden: {response.text}")
    return response.json()
