from fastapi import FastAPI, HTTPException
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
async def webhook(signal: Signal):
    try:
        # Procesar la señal de TradingView
        action = signal.action
        symbol = signal.symbol
        quantity = signal.quantity

        # Autenticar y obtener el token
        token = authenticate()

        # Ejecutar la orden en Capital.com
        if signal.action == "buy":
            place_order(token, "BUY", signal.symbol, signal.quantity)
        elif signal.action == "sell":
            place_order(token, "SELL", signal.symbol, signal.quantity)
        else:
            raise HTTPException(status_code=400, detail="Acción no válida")

        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en webhook: {str(e)}")

# Función para autenticar en Capital.com
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
    
    try:
        auth_data = response.json()
        print(f"Datos de autenticación: {auth_data}")
        
        # Usar el currentAccountId como identificador
        account_id = auth_data["currentAccountId"]
        print(f"currentAccountId obtenido: {account_id}")
        
        # Si se obtiene el account_id, lo utilizamos para la ejecución de la orden
        return account_id
    except Exception as e:
        print(f"Error al procesar la respuesta de autenticación: {e}")
        raise Exception(f"Error al procesar la respuesta de autenticación: {response.text}")

# Función para ejecutar una orden en Capital.com
def place_order(account_id: str, direction: str, epic: str, size: int):
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "type": "MARKET",  # Tipo de orden (MARKET, LIMIT, etc.)
        "currencyCode": "USD",  # Moneda de la operación
        "accountId": account_id  # Usamos el account_id obtenido
    }
    response = requests.post(f"{CAPITAL_API_URL}/orders", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al ejecutar la orden: {response.text}")
    return response.json()