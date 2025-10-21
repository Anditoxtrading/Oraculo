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
import sys
import io

# Configurar encoding UTF-8 para Windows
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

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
        "last_u": None,
        "retry_count": 0,  # Para retry exponencial
        "first_event_after_snapshot": True  # Bandera para el primer evento
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

def on_message_combined(ws, message):
    """Maneja mensajes de streams combinados"""
    try:
        parsed = json.loads(message)

        # Extraer símbolo del stream name: "btcusdt@depth@100ms" -> "BTCUSDT"
        if 'stream' not in parsed:
            return

        stream_name = parsed['stream']
        data = parsed['data']
        symbol = stream_name.split('@')[0].upper()

        with order_book_lock:
            if symbol not in order_books:
                return

            book = order_books[symbol]

            # Si no está inicializado, agregar al buffer (optimizado: consolidar eventos)
            if not book['initialized']:
                # Optimización: Si ya existe un evento que cubre este rango, eliminarlo
                # Según Binance: "Por el mismo precio, la última actualización cubre la anterior"
                book['buffer'] = [e for e in book['buffer'] if not (e['u'] < data['U'])]
                book['buffer'].append(data)
                return

            # Paso 6: Verificar continuidad (pu debe ser igual al u anterior)
            # Excepción: El primer evento después del snapshot puede tener pu < lastUpdateId
            if book['first_event_after_snapshot']:
                # Primer evento: validar que U <= lastUpdateId <= u (según docs Binance)
                if data['U'] <= book['lastUpdateId'] <= data['u']:
                    # Evento válido, procesar y desactivar bandera
                    book['first_event_after_snapshot'] = False
                    apply_order_book_update(symbol, data)
                    return
                elif data['u'] < book['lastUpdateId']:
                    # Evento antiguo, ignorar
                    return
                else:
                    # Evento no cubre el lastUpdateId, puede ser discontinuidad
                    print(f"⚠️ Primer evento no cubre lastUpdateId en {symbol}. U={data['U']}, u={data['u']}, lastUpdateId={book['lastUpdateId']}")
                    book['initialized'] = False
                    book['buffer'] = [data]
                    threading.Thread(target=reinitialize_symbol, args=(symbol,), daemon=True).start()
                    return

            # Validación normal de continuidad para eventos subsecuentes
            if data['pu'] != book['last_u']:
                print(f"⚠️ Discontinuidad detectada en {symbol}. Esperado pu={book['last_u']}, recibido pu={data['pu']}")
                # Reiniciar el proceso
                book['initialized'] = False
                book['first_event_after_snapshot'] = True
                book['buffer'] = [data]
                threading.Thread(target=reinitialize_symbol, args=(symbol,), daemon=True).start()
                return

            # Aplicar la actualización
            apply_order_book_update(symbol, data)

    except Exception as e:
        print(f"💥 Error procesando mensaje: {e}")

def reinitialize_symbol(symbol):
    """Reinicializa el order book de un símbolo"""
    print(f"🔄 Reinicializando {symbol}...")
    time.sleep(1)  # Esperar un poco antes de reinicializar
    initialize_order_book(symbol)

def initialize_order_book(symbol, retry_count=0):
    """Inicializa el order book con snapshot y procesa buffer con retry exponencial"""
    max_retries = 10
    base_delay = 1
    max_delay = 60

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
            book['retry_count'] = 0  # Reset en caso de éxito
            print(f"📸 Snapshot cargado para {symbol} (lastUpdateId: {snap['lastUpdateId']}, buffer: {len(book['buffer'])} eventos)")

        # Procesar buffer
        if not process_buffer(symbol):
            # Si falla, reintentar con backoff exponencial
            if retry_count < max_retries:
                delay = min(base_delay * (2 ** retry_count), max_delay)
                print(f"🔄 Reintentando inicialización de {symbol} en {delay}s (intento {retry_count + 1}/{max_retries})...")
                time.sleep(delay)
                initialize_order_book(symbol, retry_count + 1)
            else:
                print(f"❌ Máximo de reintentos alcanzado para {symbol}")

    except Exception as e:
        if retry_count < max_retries:
            # Retry exponencial: 1s, 2s, 4s, 8s, 16s, 32s, 60s (max)
            delay = min(base_delay * (2 ** retry_count), max_delay)
            print(f"💥 Error inicializando {symbol}: {e}")
            print(f"🔄 Reintentando en {delay}s (intento {retry_count + 1}/{max_retries})...")
            time.sleep(delay)
            initialize_order_book(symbol, retry_count + 1)
        else:
            print(f"❌ Error crítico en {symbol} después de {max_retries} intentos: {e}")

def start_individual_websockets():
    """Inicia WebSockets individuales para cada símbolo"""
    print(f"🚀 Iniciando WebSockets individuales para {len(coins)} símbolos...")

    for symbol in coins:
        threading.Thread(
            target=run_individual_websocket,
            args=(symbol,),
            daemon=True
        ).start()
        time.sleep(0.1)  # Pequeña pausa para evitar sobrecarga al inicio

def run_individual_websocket(symbol):
    """Ejecuta un WebSocket individual para un símbolo específico"""
    conexion_numero = 0

    while True:
        conexion_numero += 1
        try:
            # Crear stream individual: "btcusdt@depth@100ms"
            url = f"wss://fstream.binance.com/stream?streams={symbol.lower()}@depth@100ms"

            if conexion_numero == 1:
                print(f"🔌 [{symbol}] Iniciando WebSocket (conexión #{conexion_numero})...", flush=True)
            else:
                print(f"🔄 [{symbol}] Reconectando WebSocket (intento #{conexion_numero})...", flush=True)

            def on_open_handler(_):
                print(f"✅ [{symbol}] WebSocket conectado exitosamente", flush=True)

            def on_error_handler(_, error):
                print(f"⚠️ [{symbol}] Error WS: {error}", flush=True)

            def on_close_handler(*args):
                close_code = args[1] if len(args) > 1 else 'N/A'
                print(f"❌ [{symbol}] WebSocket desconectado (código: {close_code})", flush=True)

            ws = websocket.WebSocketApp(
                url,
                on_open=on_open_handler,
                on_message=lambda _, msg: on_message_combined(_, msg),
                on_error=on_error_handler,
                on_close=on_close_handler,
            )
            # Sin ping/pong - Binance maneja keep-alive automáticamente
            ws.run_forever()
        except Exception as e:
            print(f"💥 [{symbol}] Excepción en WebSocket: {e}")

        # Marcar el símbolo como no inicializado
        with order_book_lock:
            order_books[symbol]['initialized'] = False
            order_books[symbol]['buffer'] = []
            order_books[symbol]['first_event_after_snapshot'] = True

        print(f"⏳ [{symbol}] Esperando 5 segundos antes de reconectar...", flush=True)
        time.sleep(5)

        # Esperar a que el WebSocket se reconecte y acumule eventos
        print(f"📡 [{symbol}] Acumulando eventos del buffer...", flush=True)
        time.sleep(3)

        # Reinicializar el símbolo después de reconectar
        print(f"🔄 [{symbol}] Solicitando snapshot y reinicializando...", flush=True)
        threading.Thread(target=initialize_order_book, args=(symbol,), daemon=True).start()

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
    # Iniciar WebSockets individuales (1 conexión por símbolo)
    print("🚀 Iniciando WebSockets individuales...")
    start_individual_websockets()

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

    # Mantener vivo el proceso principal y mostrar estado cada 60 segundos
    while True:
        await asyncio.sleep(60)

        # Recopilar estadísticas detalladas
        with order_book_lock:
            initialized_count = sum(1 for b in order_books.values() if b['initialized'])
            pending_count = len(coins) - initialized_count

            # Contar símbolos con datos
            symbols_con_datos = []
            symbols_pendientes = []
            for symbol, book in order_books.items():
                if book['initialized']:
                    symbols_con_datos.append(symbol)
                else:
                    symbols_pendientes.append(symbol)

        # Mostrar resumen de estado claro
        porcentaje = (initialized_count / len(coins) * 100) if len(coins) > 0 else 0

        print("\n" + "="*80, flush=True)
        print(f"📊 ESTADO DEL SISTEMA - {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print("="*80, flush=True)
        print(f"✅ Order books inicializados: {initialized_count}/{len(coins)} ({porcentaje:.1f}%)", flush=True)
        print(f"⏳ Pendientes de inicializar: {pending_count}", flush=True)

        if initialized_count == len(coins):
            print(f"🟢 SISTEMA OPERATIVO AL 100% - Todos los order books funcionando correctamente", flush=True)
        elif initialized_count > 0:
            print(f"🟡 SISTEMA PARCIALMENTE OPERATIVO", flush=True)
            if pending_count <= 5 and pending_count > 0:
                print(f"   Símbolos pendientes: {', '.join(symbols_pendientes)}", flush=True)
        else:
            print(f"🔴 SISTEMA NO OPERATIVO - Ningún order book inicializado", flush=True)

        print(f"🌐 API REST: http://localhost:8000/orderbooks/{{symbol}}", flush=True)
        print("="*80 + "\n", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
