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
    # Obtener el JSON crudo recibido para depuración
    data = await request.json()
    print("Datos crudos recibidos:", data)
    
    try:
        # Intentar parsear los datos al modelo Signal
        signal = Signal(**data)
        print("Datos recibidos y parseados:", signal)
        
        # Procesar la señal de TradingView
        action = signal.action
        symbol = signal.symbol
        quantity = signal.quantity

        # Autenticar y obtener los tokens (CST y X-SECURITY-TOKEN)
        cst, x_security_token = authenticate()

        # Ejecutar la orden en Capital.com
        if signal.action == "buy":
            place_order(cst, x_security_token, "BUY", signal.symbol, signal.quantity)
        elif signal.action == "sell":
            place_order(cst, x_security_token, "SELL", signal.symbol, signal.quantity)
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
    
    # Imprime la respuesta completa para depuración
    print("Respuesta completa de autenticación:", response.text)
    
    if response.status_code != 200:
        raise Exception(f"Error de autenticación: {response.text}")
    
    # Intentar procesar la respuesta como JSON para obtener el CST y X-SECURITY-TOKEN
    try:
        auth_data = response.json()
    except Exception as e:
        raise Exception(f"Error al procesar JSON de respuesta: {str(e)}")
    
    print("Datos completos de autenticación:", auth_data)
    
    # Asegurarse de que CST y X-SECURITY-TOKEN estén presentes
    if 'clientId' not in auth_data or 'currentAccountId' not in auth_data:
        raise Exception("No se encontró la información necesaria para autenticarse.")

    # Obtenemos el CST y X-SECURITY-TOKEN de los encabezados de la respuesta
    cst = response.headers.get("CST")
    x_security_token = response.headers.get("X-SECURITY-TOKEN")
    
    # Verifica si ambos tokens están presentes
    if not cst or not x_security_token:
        raise Exception("No se encontraron los tokens necesarios (CST, X-SECURITY-TOKEN).")
    
    return cst, x_security_token

# Función para ejecutar una orden en Capital.com (usando '/positions' en lugar de '/orders')
def place_order(cst: str, x_security_token: str, direction: str, epic: str, size: int = 10):  # size por defecto 1
    # Validar que el tamaño de la orden sea válido
    MIN_SIZE = 100  # Define un tamaño mínimo (ajústalo según sea necesario)
    
    if size < MIN_SIZE:
        raise Exception(f"El tamaño mínimo de la orden es {MIN_SIZE}. Estás intentando operar con un tamaño de {size}.")
    
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": cst,  # Se usa el CST para la autorización
        "X-SECURITY-TOKEN": x_security_token,  # Se usa el X-SECURITY-TOKEN
        "Content-Type": "application/json"
    }
    
    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,  # Tamaño de la orden
        "type": "MARKET",  # Tipo de orden (MARKET, LIMIT, etc.)
        "currencyCode": "USD"  # Moneda de la operación
    }

    # Enviar la solicitud para abrir una posición
    response = requests.post(f"{CAPITAL_API_URL}/positions", headers=headers, json=payload)
    
    # Revisar si la solicitud fue exitosa
    if response.status_code != 200:
        raise Exception(f"Error al ejecutar la orden: {response.text}")
    
    return response.json()