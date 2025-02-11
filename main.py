from fastapi import FastAPI, HTTPException
import requests

app = FastAPI()

# Configuración de la API de Capital.com
CAPITAL_API_URL = "https://api-capital.backend-capital.com/api/v1"
API_KEY = "5s6AT3Ka4UQEt8TI"
ACCOUNT_ID = "eddrd89@outlook.com"

# Endpoint para recibir alertas de TradingView
@app.post("/webhook")
async def webhook(signal: dict):
    try:
        # Procesar la señal de TradingView
        action = signal.get("{{strategy.order.action}}")  # "buy" o "sell"
        symbol = signal.get("{{ticker}}")  # Símbolo del instrumento (ejemplo: "EURUSD")
        quantity = signal.get("quantity", 1)  # Cantidad a operar (por defecto: 1)

        # Ejecutar la orden en Capital.com
        if action == "buy":
            place_order("BUY", symbol, quantity)
        elif action == "sell":
            place_order("SELL", symbol, quantity)
        else:
            raise HTTPException(status_code=400, detail="Acción no válida")

        return {"message": "Orden ejecutada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Función para ejecutar una orden en Capital.com
def place_order(direction: str, epic: str, size: int):
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "type": "MARKET",  # Tipo de orden (MARKET, LIMIT, etc.)
        "currencyCode": "USD"  # Moneda de la operación
    }
    response = requests.post(f"{CAPITAL_API_URL}/orders", headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Error al ejecutar la orden: {response.text}")
    return response.json()
