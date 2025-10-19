import websocket
import json
import requests
import threading
import asyncio
import time
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from binance.client import Client
from collections import OrderedDict

# ===== CONFIGURACIÓN BINANCE =====
api_key = ''
api_secret = ''
client = Client(api_key=api_key, api_secret=api_secret)

# Lista final de monedas perpetuas válidas
coins = []

# 1️⃣ Obtener información completa de contratos de futuros
exchange_info = client.futures_exchange_info()

# 2️⃣ Filtrar solo los contratos PERPETUAL activos en USDT
perpetual_symbols = []
for s in exchange_info['symbols']:
    if (
        s['contractType'] == 'PERPETUAL'
        and s['quoteAsset'] == 'USDT'
        and s['status'] == 'TRADING'  # activos
    ):
        perpetual_symbols.append(s['symbol'])

# 3️⃣ Obtener los tickers y cruzar con los perpetuos válidos
futures_info = client.futures_ticker()

for el in futures_info:
    symbol = el['symbol']
    if (
        symbol in perpetual_symbols
        and float(el.get('quoteVolume', 0)) > 200_000_000
        and float(el.get('lastPrice', 0)) < 40
    ):
        coins.append(symbol)

print(f"✅ Se encontraron {len(coins)} monedas de Futuros PERPETUOS válidas:")
print(coins)

# Estructura mejorada para los libros de órdenes
order_books = {
    symbol: {
        "bids": OrderedDict(),
        "asks": OrderedDict(),
        "lastUpdateId": None,
        "buffer": [],
        "initialized": False,
        "last_u": None
    } for symbol in coins
}
order_book_lock = threading.Lock()

print(f"Monedas de futuros monitoreadas: {coins}")

# ===== FUNCIONES DE ORDEN BOOK =====
def get_order_book_snapshot(symbol):
    url = f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=1000"
    response = requests.get(url)
    return response.json()

def process_buffer(symbol):
    """Procesa el buffer de eventos después de cargar el snapshot"""
    with order_book_lock:
        book = order_books[symbol]
        lastUpdateId = book['lastUpdateId']
        
        # Paso 4: Descartar eventos donde u < lastUpdateId
        book['buffer'] = [e for e in book['buffer'] if e['u'] >= lastUpdateId]
        
        # Paso 5: El primer evento debe tener U <= lastUpdateId AND u >= lastUpdateId
        if not book['buffer']:
            # Buffer vacío es normal en monedas de bajo volumen
            # Simplemente marcamos como inicializado y esperamos el siguiente evento
            book['initialized'] = True
            book['last_u'] = lastUpdateId
            print(f"✅ Order book inicializado (esperando eventos): {symbol}")
            return True
        
        first_event = book['buffer'][0]
        if not (first_event['U'] <= lastUpdateId <= first_event['u']):
            print(f"⚠️ Secuencia incorrecta para {symbol}. U={first_event['U']}, u={first_event['u']}, lastUpdateId={lastUpdateId}")
            return False
        
        # Procesar todos los eventos del buffer
        for event in book['buffer']:
            apply_order_book_update(symbol, event)
        
        book['buffer'] = []
        book['initialized'] = True
        print(f"✅ Order book inicializado correctamente: {symbol}")
        return True

def apply_order_book_update(symbol, data):
    """Aplica una actualización al order book"""
    book = order_books[symbol]
    
    # Actualizar bids
    for price, qty in data['b']:
        price_str = price
        qty_float = float(qty)
        if qty_float == 0:
            book['bids'].pop(price_str, None)
        else:
            book['bids'][price_str] = qty
    
    # Actualizar asks
    for price, qty in data['a']:
        price_str = price
        qty_float = float(qty)
        if qty_float == 0:
            book['asks'].pop(price_str, None)
        else:
            book['asks'][price_str] = qty
    
    # Actualizar last_u para verificación de continuidad
    book['last_u'] = data['u']

def on_message(ws, message, symbol):
    try:
        data = json.loads(message)['data']
        
        with order_book_lock:
            book = order_books[symbol]
            
            # Si no está inicializado, agregar al buffer
            if not book['initialized']:
                book['buffer'].append(data)
                return
            
            # Si acabamos de inicializar y last_u es el lastUpdateId del snapshot,
            # validar que este sea el primer evento después del snapshot
            if book['last_u'] == book['lastUpdateId']:
                # Primer evento después del snapshot
                if data['U'] <= book['lastUpdateId'] <= data['u']:
                    # Evento válido, aplicar y continuar
                    apply_order_book_update(symbol, data)
                    return
                elif data['u'] < book['lastUpdateId']:
                    # Evento antiguo, ignorar
                    return
                # Si no cumple las condiciones, continuar con verificación normal
            
            # Paso 6: Verificar continuidad (pu debe ser igual al u anterior)
            if data['pu'] != book['last_u']:
                print(f"⚠️ Discontinuidad detectada en {symbol}. Esperado pu={book['last_u']}, recibido pu={data['pu']}")
                # Reiniciar el proceso
                book['initialized'] = False
                book['buffer'] = [data]
                threading.Thread(target=reinitialize_symbol, args=(symbol,), daemon=True).start()
                return
            
            # Aplicar la actualización
            apply_order_book_update(symbol, data)
            
    except Exception as e:
        print(f"💥 Error procesando mensaje para {symbol}: {e}")

def reinitialize_symbol(symbol):
    """Reinicializa el order book de un símbolo"""
    print(f"🔄 Reinicializando {symbol}...")
    time.sleep(1)  # Esperar un poco antes de reinicializar
    initialize_order_book(symbol)

def initialize_order_book(symbol):
    """Inicializa el order book con snapshot y procesa buffer"""
    try:
        # Esperar un poco para acumular eventos en el buffer
        time.sleep(3)
        
        # Paso 3: Obtener snapshot
        snap = get_order_book_snapshot(symbol)
        
        with order_book_lock:
            book = order_books[symbol]
            
            # Limpiar order book
            book['bids'].clear()
            book['asks'].clear()
            
            # Cargar snapshot
            for bid in snap['bids']:
                book['bids'][bid[0]] = bid[1]
            for ask in snap['asks']:
                book['asks'][ask[0]] = ask[1]
            
            book['lastUpdateId'] = snap['lastUpdateId']
            print(f"📸 Snapshot cargado para {symbol} (lastUpdateId: {snap['lastUpdateId']}, buffer: {len(book['buffer'])} eventos)")
        
        # Procesar buffer
        if not process_buffer(symbol):
            # Si falla, reintentar
            print(f"🔄 Reintentando inicialización de {symbol}...")
            time.sleep(2)
            initialize_order_book(symbol)
            
    except Exception as e:
        print(f"💥 Error inicializando {symbol}: {e}")
        time.sleep(5)
        initialize_order_book(symbol)

def start_websocket(symbol):
    """Conexión WebSocket con reconexión automática"""
    while True:
        try:
            print(f"🔌 Conectando WebSocket para {symbol}...")
            ws = websocket.WebSocketApp(
                f"wss://fstream.binance.com/stream?streams={symbol.lower()}@depth@100ms",
                on_message=lambda ws, msg: on_message(ws, msg, symbol),
                on_error=lambda ws, err: print(f"⚠️ Error WS {symbol}: {err}"),
                on_close=lambda ws, code, msg: print(f"❌ WS cerrado {symbol}: {msg}"),
            )
            ws.run_forever()
        except Exception as e:
            print(f"💥 Error en WS {symbol}: {e}")
        
        # Marcar como no inicializado para reiniciar
        with order_book_lock:
            order_books[symbol]['initialized'] = False
            order_books[symbol]['buffer'] = []
        
        print(f"🔁 Reintentando conexión para {symbol} en 5 segundos...")
        time.sleep(5)

# ===== API LOCAL (FastAPI) =====
app = FastAPI()

@app.get("/orderbooks/{symbol}")
def get_orderbook(symbol: str):
    symbol = symbol.upper()
    if symbol not in order_books:
        return JSONResponse({"error": "Símbolo no monitoreado"}, status_code=404)
    
    with order_book_lock:
        book = order_books[symbol]
        if not book['initialized']:
            return JSONResponse({"error": "Order book aún no inicializado"}, status_code=503)
        
        # Convertir a diccionarios para compatibilidad con el bot de análisis
        bids_dict = {price: qty for price, qty in book['bids'].items()}
        asks_dict = {price: qty for price, qty in book['asks'].items()}
        
        return JSONResponse({
            "symbol": symbol,
            "bids": bids_dict,
            "asks": asks_dict,
            "lastUpdateId": book['lastUpdateId'],
            "last_u": book['last_u']
        })

@app.get("/symbols")
def get_symbols():
    with order_book_lock:
        initialized = [s for s, b in order_books.items() if b['initialized']]
        pending = [s for s, b in order_books.items() if not b['initialized']]
    
    return {
        "symbols": list(order_books.keys()),
        "initialized": initialized,
        "pending": pending
    }

# ===== MAIN =====
async def main():
    # Iniciar WebSockets primero (paso 1)
    for symbol in coins:
        threading.Thread(target=start_websocket, args=(symbol,), daemon=True).start()
    
    # Esperar para que empiecen a llegar eventos y se acumulen en el buffer
    print("⏳ Esperando acumulación de eventos...")
    await asyncio.sleep(5)
    
    # Cargar snapshots e inicializar (pasos 2-5)
    for symbol in coins:
        threading.Thread(target=initialize_order_book, args=(symbol,), daemon=True).start()
        await asyncio.sleep(0.2)  # Escalonar las peticiones
    
    # Iniciar la API en otro hilo independiente
    def start_api():
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    
    threading.Thread(target=start_api, daemon=True).start()
    
    print("🚀 API de OrderBooks corriendo en http://localhost:8000")
    
    # Mantener vivo el proceso principal
    while True:
        await asyncio.sleep(60)
        # Mostrar estado cada minuto
        with order_book_lock:
            initialized_count = sum(1 for b in order_books.values() if b['initialized'])
        print(f"📊 Estado: {initialized_count}/{len(coins)} order books inicializados")

if __name__ == "__main__":
    asyncio.run(main())