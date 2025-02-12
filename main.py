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
    
    # Realizar la autenticación
    response = requests.post(f"{CAPITAL_API_URL}/session", headers=headers, json=payload)
    
    if response.status_code != 200:
        raise Exception(f"Error de autenticación: {response.text}")

    # Obtener CST y X-SECURITY-TOKEN desde los encabezados de la respuesta
    cst = response.headers.get("CST")
    security_token = response.headers.get("X-SECURITY-TOKEN")
    
    if not cst or not security_token:
        raise Exception("No se obtuvo CST o X-SECURITY-TOKEN. Verifica los encabezados de la respuesta.")
    
    print(f"Autenticación exitosa: CST={cst}, X-SECURITY-TOKEN={security_token}")
    
    return cst, security_token  # Devuelvo ambos valores para usarlos en las solicitudes posteriores

# Función para ejecutar una orden en Capital.com
def place_order(cst: str, security_token: str, direction: str, epic: str, size: int):
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "Content-Type": "application/json",
        "CST": cst,
        "X-SECURITY-TOKEN": security_token
    }
    
    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,  # Se debe pasar el tamaño de la orden aquí
        "type": "MARKET",  # Tipo de orden (MARKET, LIMIT, etc.)
        "currencyCode": "USD"  # Moneda de la operación
    }
    
    # Realizar la solicitud para ejecutar la orden
    response = requests.post(f"{CAPITAL_API_URL}/orders", headers=headers, json=payload)
    
    if response.status_code != 200:
        raise Exception(f"Error al ejecutar la orden: {response.text}")
    
    return response.json()  # Devuelvo la respuesta de la orden para mayor detalle

# Función que maneja el webhook y ejecuta las órdenes
@app.post("/webhook")
async def webhook(signal: Signal):
    try:
        # Autenticar y obtener CST y X-SECURITY-TOKEN
        cst, security_token = authenticate()

        # Verificamos que la cantidad (size) esté bien definida
        if signal.quantity <= 0:
            raise HTTPException(status_code=400, detail="La cantidad debe ser mayor que 0.")

        # Ejecutar la orden en Capital.com
        if signal.action == "buy":
            place_order(cst, security_token, "BUY", signal.symbol, signal.quantity)  # El tamaño se pasa aquí
        elif signal.action == "sell":
            place_order(cst, security_token, "SELL", signal.symbol, signal.quantity)  # El tamaño se pasa aquí
        else:
            raise HTTPException(status_code=400, detail="Acción no válida")

        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en webhook: {str(e)}")

