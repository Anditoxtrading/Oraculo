import tkinter as tk
from tkinter import ttk
import asyncio
import requests
import json
import os
from collections import defaultdict
from decimal import Decimal, ROUND_DOWN
import threading
import time
import websocket
import sys
import io

# Configurar encoding UTF-8 para Windows
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ---------- FUNCIONES UTILITARIAS ----------

def formatear_volumen(num):
    if num >= 1_000_000_000:
        return f"{num / 1_000_000_000:.1f}b"
    elif num >= 1_000_000:
        return f"{num / 1_000_000:.1f}m"
    elif num >= 1_000:
        return f"{num / 1_000:.1f}k"
    else:
        return f"{num:.2f}"

def obtener_decimales_de_tick(tick_size):
    tick_str = f"{tick_size:.10f}".rstrip('0')
    if '.' not in tick_str:
        return 0
    return len(tick_str.split('.')[1])

def obtener_nivel_agrupacion_optimo(tick_size, precio_actual):
    try:
        if precio_actual is None or precio_actual <= 0:
            return tick_size
            
        if precio_actual >= 100:
            agrupacion_base = 10.0
        elif precio_actual >= 10:
            agrupacion_base = 1.0
        elif precio_actual >= 1:
            agrupacion_base = 0.1
        elif precio_actual >= 0.1:
            agrupacion_base = 0.01
        elif precio_actual >= 0.01:
            agrupacion_base = 0.001
        elif precio_actual >= 0.001:
            agrupacion_base = 0.0001
        else:
            agrupacion_base = 0.00001
        
        tick_decimal = Decimal(str(tick_size))
        agrupacion_decimal = Decimal(str(agrupacion_base))
        cociente = agrupacion_decimal / tick_decimal
        
        if cociente % 1 == 0:
            return agrupacion_base
        
        niveles_posibles = [0.00001, 0.0001, 0.001, 0.01, 0.1, 1, 10, 100]
        
        for nivel in reversed(niveles_posibles):
            nivel_decimal = Decimal(str(nivel))
            cociente = nivel_decimal / tick_decimal
            if cociente % 1 == 0 and nivel <= agrupacion_base:
                return nivel
        
        return tick_size
        
    except Exception as e:
        return tick_size

def agrupar_precio_binance(price, agrupacion):
    price_decimal = Decimal(str(price))
    agrupacion_decimal = Decimal(str(agrupacion))
    agrupado = (price_decimal / agrupacion_decimal).quantize(Decimal('1'), rounding=ROUND_DOWN) * agrupacion_decimal
    return float(agrupado)

# ---------- OBTENER DATOS BINANCE ----------

# Diccionario global para almacenar precios en tiempo real desde WebSocket
precios_websocket = {}
precios_lock = threading.Lock()

def obtener_precio_actual(symbol):
    """Obtiene el precio desde el WebSocket en memoria (sin REST API)"""
    with precios_lock:
        return precios_websocket.get(symbol)

def iniciar_websocket_precios(symbols):
    """Inicia WebSocket combinado para obtener precios en tiempo real"""
    def on_message_precio(ws, message):
        try:
            data = json.loads(message)
            if 'stream' in data:
                stream_data = data['data']
                symbol = stream_data['s']  # Símbolo
                precio = float(stream_data['c'])  # Close price (precio actual)

                with precios_lock:
                    precios_websocket[symbol] = precio
        except Exception as e:
            pass

    def run_ws_precios():
        while True:
            try:
                # Crear streams combinados para ticker: btcusdt@ticker/ethusdt@ticker/...
                streams = '/'.join([f"{symbol.lower()}@ticker" for symbol in symbols])
                url = f"wss://fstream.binance.com/stream?streams={streams}"

                print(f"🔌 Conectando WebSocket de precios ({len(symbols)} símbolos)...")

                ws = websocket.WebSocketApp(
                    url,
                    on_message=on_message_precio,
                    on_error=lambda _, err: print(f"⚠️ Error WS precios: {err}"),
                    on_close=lambda _, __, msg: print(f"❌ WS precios cerrado"),
                )
                ws.run_forever()
            except Exception as e:
                print(f"💥 Error en WS precios: {e}")

            print("🔁 Reconectando WS precios en 5 segundos...")
            time.sleep(5)

    # Iniciar en hilo separado
    threading.Thread(target=run_ws_precios, daemon=True).start()

def obtener_tick_size(symbol):
    url = f"https://fapi.binance.com/fapi/v1/exchangeInfo"
    try:
        data = requests.get(url, timeout=5).json()
        for s in data["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "PRICE_FILTER":
                        return float(f["tickSize"])
        return 0.01
    except Exception as e:
        return 0.01

def cargar_libro_ordenes_api(symbols, base_url="http://localhost:8000"):
    order_books = {}
    for symbol in symbols:
        try:
            resp = requests.get(f"{base_url}/orderbooks/{symbol}", timeout=5)
            if resp.status_code == 200:
                order_books[symbol] = resp.json()
        except Exception as e:
            pass
    return order_books

def obtener_simbolos(base_url="http://localhost:8000"):
    try:
        resp = requests.get(f"{base_url}/symbols", timeout=5)
        if resp.status_code == 200:
            return resp.json().get("symbols", [])
    except Exception as e:
        pass
    return []

# ---------- ANÁLISIS DE SHOCKS ----------

def calcular_shocks(order_book, agrupacion, tick_size):
    bid_ranges = defaultdict(lambda: {'total_qty': 0, 'price_count': {}})
    ask_ranges = defaultdict(lambda: {'total_qty': 0, 'price_count': {}})

    for price, qty in order_book.get('bids', {}).items():
        price, qty = float(price), float(qty)
        range_key = agrupar_precio_binance(price, agrupacion)
        bid_ranges[range_key]['total_qty'] += qty
        bid_ranges[range_key]['price_count'][price] = bid_ranges[range_key]['price_count'].get(price, 0) + qty

    for price, qty in order_book.get('asks', {}).items():
        price, qty = float(price), float(qty)
        range_key = agrupar_precio_binance(price, agrupacion)
        ask_ranges[range_key]['total_qty'] += qty
        ask_ranges[range_key]['price_count'][price] = ask_ranges[range_key]['price_count'].get(price, 0) + qty

    decimales_tick = obtener_decimales_de_tick(tick_size)

    top_bids = sorted(bid_ranges.items(), key=lambda x: x[1]['total_qty'], reverse=True)[:6]
    top_asks = sorted(ask_ranges.items(), key=lambda x: x[1]['total_qty'], reverse=True)[:6]

    shocks_long = []
    for pr_range, data in top_bids:
        total_qty = data['total_qty']
        if total_qty > 0:
            weighted_avg_price = sum(p * q for p, q in data['price_count'].items()) / total_qty
            weighted_avg_price = agrupar_precio_binance(weighted_avg_price, tick_size)
            shocks_long.append(weighted_avg_price)

    shocks_short = []
    for pr_range, data in top_asks:
        total_qty = data['total_qty']
        if total_qty > 0:
            weighted_avg_price = sum(p * q for p, q in data['price_count'].items()) / total_qty
            weighted_avg_price = agrupar_precio_binance(weighted_avg_price, tick_size)
            shocks_short.append(weighted_avg_price)

    shocks_long.sort(reverse=True)
    shocks_short.sort()

    return shocks_long, shocks_short, decimales_tick

# ---------- INTERFAZ GRÁFICA ----------

class ShockDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("📊 Dashboard de Shocks - Trading Bot")
        self.root.geometry("1800x900")
        self.root.configure(bg="#0f0f1e")
        
        # Variables
        self.agrupaciones = {}
        self.tick_sizes = {}
        self.base_url = "http://localhost:8000"
        self.tarjetas_activas = {}
        self.precios_actuales = {}
        self.shocks_activos = {}
        self.actualizando = True
        self.precio_anterior = {}
        self.hilos_monitores = {}
        self.animaciones_activas = {}
        self.actualizaciones_pendientes = set()
        self.ultimo_reorden = 0
        
        # Crear interfaz
        self.crear_interfaz()
        
        # Iniciar escaneo inicial
        self.escaneo_inicial()

        # Iniciar procesador de actualizaciones agrupadas (60 FPS = ~16ms)
        self.procesar_actualizaciones_agrupadas()
    
    def crear_interfaz(self):
        # Header compacto
        header = tk.Frame(self.root, bg="#1a1a2e", height=40)
        header.pack(fill=tk.X, padx=10, pady=(5, 5))
        
        # Stats compactas en una sola línea
        stats_frame = tk.Frame(header, bg="#1a1a2e")
        stats_frame.pack(pady=8)
        
        self.lbl_total = tk.Label(stats_frame, text="Total: 0", 
                                  font=("Segoe UI", 10), 
                                  bg="#1a1a2e", fg="#ffffff")
        self.lbl_total.pack(side=tk.LEFT, padx=15)
        
        self.lbl_longs = tk.Label(stats_frame, text="Longs: 0", 
                                  font=("Segoe UI", 10), 
                                  bg="#1a1a2e", fg="#00ff88")
        self.lbl_longs.pack(side=tk.LEFT, padx=15)
        
        self.lbl_shorts = tk.Label(stats_frame, text="Shorts: 0", 
                                   font=("Segoe UI", 10), 
                                   bg="#1a1a2e", fg="#ff4444")
        self.lbl_shorts.pack(side=tk.LEFT, padx=15)
        
        self.lbl_status = tk.Label(stats_frame, text="🟢 Monitoreando", 
                                   font=("Segoe UI", 9), 
                                   bg="#1a1a2e", fg="#00ff88")
        self.lbl_status.pack(side=tk.LEFT, padx=15)
        
        # Contenedor de columnas
        columnas = tk.Frame(self.root, bg="#0f0f1e")
        columnas.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Columna LONG (Verde)
        long_frame = tk.Frame(columnas, bg="#0f0f1e")
        long_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        long_header = tk.Label(long_frame, text="🟢 LONG", 
                               font=("Segoe UI", 13, "bold"),
                               bg="#00cc66", fg="#ffffff", 
                               pady=8)
        long_header.pack(fill=tk.X)
        
        # Scroll para longs
        long_scroll_frame = tk.Frame(long_frame, bg="#1a1a2e")
        long_scroll_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.long_canvas = tk.Canvas(long_scroll_frame, bg="#1a1a2e", highlightthickness=0)
        long_scrollbar = ttk.Scrollbar(long_scroll_frame, orient="vertical", command=self.long_canvas.yview)
        self.long_container = tk.Frame(self.long_canvas, bg="#1a1a2e")
        
        # Optimizado: reduce llamadas al scrollregion
        self.long_container.bind("<Configure>",
                                lambda e: self.actualizar_scrollregion_debounced(self.long_canvas))
        
        self.long_canvas.create_window((0, 0), window=self.long_container, anchor="nw")
        self.long_canvas.configure(yscrollcommand=long_scrollbar.set)
        
        self.long_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        long_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Columna SHORT (Rojo)
        short_frame = tk.Frame(columnas, bg="#0f0f1e")
        short_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        short_header = tk.Label(short_frame, text="🔴 SHORT", 
                                font=("Segoe UI", 13, "bold"),
                                bg="#cc0000", fg="#ffffff", 
                                pady=8)
        short_header.pack(fill=tk.X)
        
        # Scroll para shorts
        short_scroll_frame = tk.Frame(short_frame, bg="#1a1a2e")
        short_scroll_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.short_canvas = tk.Canvas(short_scroll_frame, bg="#1a1a2e", highlightthickness=0)
        short_scrollbar = ttk.Scrollbar(short_scroll_frame, orient="vertical", command=self.short_canvas.yview)
        self.short_container = tk.Frame(self.short_canvas, bg="#1a1a2e")
        
        # Optimizado: reduce llamadas al scrollregion
        self.short_container.bind("<Configure>",
                                 lambda e: self.actualizar_scrollregion_debounced(self.short_canvas))
        
        self.short_canvas.create_window((0, 0), window=self.short_container, anchor="nw")
        self.short_canvas.configure(yscrollcommand=short_scrollbar.set)
        
        self.short_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        short_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    
    def crear_tarjeta_shock(self, container, data, tipo):
        symbol = data['symbol']
        key = f"{symbol}_{tipo}"
        
        card = tk.Frame(container, bg="#16213e", relief=tk.RAISED, bd=1)
        card.pack(fill=tk.X, padx=8, pady=4)
        
        color = "#00ff88" if tipo == "LONG" else "#ff4444"
        
        info_frame = tk.Frame(card, bg="#16213e")
        info_frame.pack(fill=tk.X, padx=12, pady=8)
        
        formato = f".{data['decimales']}f"
        
        tk.Label(info_frame, text="Moneda:", 
                font=("Segoe UI", 8, "bold"),
                bg="#16213e", fg="#888888").pack(side=tk.LEFT, padx=(0, 3))
        
        symbol_label = tk.Label(info_frame, text=symbol,
                               font=("Segoe UI", 9, "bold"),
                               bg="#16213e", fg=color,
                               cursor="hand2")
        symbol_label.pack(side=tk.LEFT, padx=(0, 12))
        # Optimizado: usar command pattern en vez de lambda para evitar closures
        symbol_label.bind("<Button-1>", lambda e, t=symbol: self.copiar_al_portapapeles(t))
        
        tk.Label(info_frame, text="Shock:", 
                font=("Segoe UI", 8, "bold"),
                bg="#16213e", fg="#888888").pack(side=tk.LEFT, padx=(0, 3))
        
        entrada_str = f"${data['entrada']:{formato}}"
        entrada_valor = f"{data['entrada']:{formato}}"
        entrada_label = tk.Label(info_frame, text=entrada_str,
                                font=("Courier New", 9, "bold"),
                                bg="#16213e", fg=color,
                                cursor="hand2")
        entrada_label.pack(side=tk.LEFT, padx=(0, 12))
        entrada_label.bind("<Button-1>", lambda e, t=entrada_valor: self.copiar_al_portapapeles(t))
        
        tk.Label(info_frame, text="Stop:", 
                font=("Segoe UI", 8, "bold"),
                bg="#16213e", fg="#888888").pack(side=tk.LEFT, padx=(0, 3))
        
        stop_str = f"${data['stop_loss']:{formato}}"
        stop_valor = f"{data['stop_loss']:{formato}}"
        stop_label = tk.Label(info_frame, text=stop_str,
                             font=("Courier New", 9, "bold"),
                             bg="#16213e", fg="#ffa500",
                             cursor="hand2")
        stop_label.pack(side=tk.LEFT, padx=(0, 3))
        stop_label.bind("<Button-1>", lambda e, t=stop_valor: self.copiar_al_portapapeles(t))
        
        entrada = data['entrada']
        stop_loss = data['stop_loss']
        distancia_entrada_stop_pct = abs((stop_loss - entrada) / entrada * 100)
        
        stop_dist_label = tk.Label(info_frame, text=f"{distancia_entrada_stop_pct:.2f}%",
                                   font=("Courier New", 9, "bold"),
                                   bg="#16213e", fg="#ff6b6b")
        stop_dist_label.pack(side=tk.LEFT, padx=(0, 12))
        
        tk.Label(info_frame, text="Dist:", 
                font=("Segoe UI", 8, "bold"),
                bg="#16213e", fg="#888888").pack(side=tk.LEFT, padx=(0, 3))
        
        dist_label = tk.Label(info_frame, text=f"{data['distancia_pct']:.2f}%", 
                             font=("Courier New", 9, "bold"),
                             bg="#16213e", fg="#00ccff")
        dist_label.pack(side=tk.LEFT)
        
        self.tarjetas_activas[key] = {
            'frame': card,
            'dist_label': dist_label,
            'data': data,
            'color': color
        }
        
        return card
    
    def procesar_actualizaciones_agrupadas(self):
        """Procesa todas las actualizaciones pendientes en un solo ciclo de UI"""
        if not self.actualizando:
            return

        # Procesar todas las actualizaciones pendientes
        if self.actualizaciones_pendientes:
            symbols_a_actualizar = list(self.actualizaciones_pendientes)
            self.actualizaciones_pendientes.clear()

            for symbol in symbols_a_actualizar:
                self.actualizar_distancia_moneda(symbol)

        # Reprogramar para el próximo ciclo (60 FPS)
        self.root.after(16, self.procesar_actualizaciones_agrupadas)

    def actualizar_scrollregion_debounced(self, canvas):
        """Actualiza el scrollregion con debouncing para evitar lag"""
        if not hasattr(self, '_scroll_update_id'):
            self._scroll_update_id = {}

        canvas_id = str(canvas)

        # Cancelar actualización pendiente
        if canvas_id in self._scroll_update_id:
            self.root.after_cancel(self._scroll_update_id[canvas_id])

        # Programar nueva actualización después de 100ms
        self._scroll_update_id[canvas_id] = self.root.after(
            100, lambda: canvas.configure(scrollregion=canvas.bbox("all"))
        )

    def copiar_al_portapapeles(self, texto):
        self.root.clipboard_clear()
        self.root.clipboard_append(texto)
        self.actualizar_status(f"📋 Copiado: {texto}")
    
    def escaneo_inicial(self):
        def escanear():
            print("🔍 Realizando escaneo inicial de order books...")
            self.actualizar_status("🔍 Escaneando...")

            symbols = obtener_simbolos(self.base_url)
            if not symbols:
                print("❌ No hay símbolos disponibles")
                self.root.after(10000, self.escaneo_inicial)
                return

            # Iniciar WebSocket de precios para todos los símbolos (UNA SOLA VEZ)
            if not hasattr(self, 'ws_precios_iniciado'):
                print(f"🚀 Iniciando WebSocket de precios para {len(symbols)} símbolos...")
                iniciar_websocket_precios(symbols)
                self.ws_precios_iniciado = True
                # Esperar 5 segundos para que se conecte y reciba primeros precios
                print("⏳ Esperando 5 segundos para recibir precios del WebSocket...")
                time.sleep(5)

                # Verificar cuántos precios se recibieron
                with precios_lock:
                    precios_recibidos = len(precios_websocket)
                print(f"✅ Precios recibidos: {precios_recibidos}/{len(symbols)}")

            for sym in symbols:
                if sym not in self.tick_sizes:
                    self.tick_sizes[sym] = obtener_tick_size(sym)
                    precio = obtener_precio_actual(sym)
                    if precio:
                        agrupacion_optima = obtener_nivel_agrupacion_optimo(self.tick_sizes[sym], precio)
                        self.agrupaciones[sym] = agrupacion_optima
            
            order_books = cargar_libro_ordenes_api(symbols, self.base_url)
            if not order_books:
                print("❌ No hay datos de libros de órdenes")
                self.root.after(10000, self.escaneo_inicial)
                return
            
            resultados_long = []
            resultados_short = []
            
            for symbol in symbols:
                if symbol not in order_books:
                    continue
                
                order_book = order_books[symbol]
                agrupacion = self.agrupaciones[symbol]
                tick = self.tick_sizes[symbol]
                precio_actual = obtener_precio_actual(symbol)
                
                if precio_actual is None:
                    continue
                
                self.precios_actuales[symbol] = precio_actual
                self.precio_anterior[symbol] = precio_actual
                
                shocks_long, shocks_short, decimales_tick = calcular_shocks(
                    order_book, agrupacion, tick)
                
                if symbol not in self.shocks_activos:
                    self.shocks_activos[symbol] = {}
                
                if len(shocks_long) >= 6:
                    shock_1_long = shocks_long[3]
                    shock_2_long = shocks_long[4]
                    distancia_pct_long = abs((shock_1_long - precio_actual) / precio_actual * 100)
                    
                    self.shocks_activos[symbol]['long'] = {
                        'entrada': shock_1_long,
                        'stop': shock_2_long
                    }
                    
                    resultados_long.append({
                        'symbol': symbol,
                        'tipo': 'LONG',
                        'entrada': shock_1_long,
                        'stop_loss': shock_2_long,
                        'distancia_pct': distancia_pct_long,
                        'precio_actual': precio_actual,
                        'decimales': decimales_tick,
                        'agrupacion': agrupacion,
                        'tick_size': tick
                    })
                
                if len(shocks_short) >= 6:
                    shock_1_short = shocks_short[3]
                    shock_2_short = shocks_short[4]
                    distancia_pct_short = abs((shock_1_short - precio_actual) / precio_actual * 100)
                    
                    self.shocks_activos[symbol]['short'] = {
                        'entrada': shock_1_short,
                        'stop': shock_2_short
                    }
                    
                    resultados_short.append({
                        'symbol': symbol,
                        'tipo': 'SHORT',
                        'entrada': shock_1_short,
                        'stop_loss': shock_2_short,
                        'distancia_pct': distancia_pct_short,
                        'precio_actual': precio_actual,
                        'decimales': decimales_tick,
                        'agrupacion': agrupacion,
                        'tick_size': tick
                    })
            
            resultados_long.sort(key=lambda x: x['distancia_pct'])
            resultados_short.sort(key=lambda x: x['distancia_pct'])
            
            print(f"✅ Escaneo inicial completado: {len(resultados_long)} LONGs, {len(resultados_short)} SHORTs")
            
            self.root.after(0, lambda: self.actualizar_ui(resultados_long, resultados_short))
            self.actualizar_status("🟢 Monitoreando")
            
            self.root.after(0, self.iniciar_hilos_monitores)
        
        threading.Thread(target=escanear, daemon=True).start()
    
    def iniciar_hilos_monitores(self):
        for symbol in self.shocks_activos.keys():
            if symbol not in self.hilos_monitores:
                hilo = threading.Thread(target=self.monitorear_moneda, args=(symbol,), daemon=True)
                hilo.start()
                self.hilos_monitores[symbol] = hilo
                print(f"🔍 Hilo de monitoreo iniciado para {symbol}")
    
    def monitorear_moneda(self, symbol):
        print(f"▶️ Iniciando monitoreo de {symbol}")

        while self.actualizando:
            try:
                # Obtener precio del WebSocket (sin REST API)
                precio_actual = obtener_precio_actual(symbol)

                if precio_actual is None:
                    time.sleep(0.5)
                    continue

                precio_prev = self.precio_anterior.get(symbol, precio_actual)
                self.precios_actuales[symbol] = precio_actual
                
                if 'long' in self.shocks_activos.get(symbol, {}):
                    entrada_long = self.shocks_activos[symbol]['long']['entrada']
                    
                    if precio_prev > entrada_long and precio_actual <= entrada_long:
                        print(f"🎯 TOQUE LONG detectado en {symbol} - Precio: {precio_actual}, Entrada: {entrada_long}")
                        self.actualizar_status(f"🎯 TOQUE LONG: {symbol}")
                        self.recalcular_shock_individual(symbol)
                
                if 'short' in self.shocks_activos.get(symbol, {}):
                    entrada_short = self.shocks_activos[symbol]['short']['entrada']
                    
                    if precio_prev < entrada_short and precio_actual >= entrada_short:
                        print(f"🎯 TOQUE SHORT detectado en {symbol} - Precio: {precio_actual}, Entrada: {entrada_short}")
                        self.actualizar_status(f"🎯 TOQUE SHORT: {symbol}")
                        self.recalcular_shock_individual(symbol)
                
                self.precio_anterior[symbol] = precio_actual

                # OPTIMIZACIÓN: Agrupar actualizaciones pendientes en vez de llamar after() cada segundo
                self.actualizaciones_pendientes.add(symbol)

            except Exception as e:
                print(f"Error monitoreando {symbol}: {e}")

            time.sleep(1)
        
        print(f"⏹️ Monitoreo detenido para {symbol}")
    
    def actualizar_distancia_moneda(self, symbol):
        """Actualiza la distancia y reordena si es necesario - OPTIMIZADO"""
        if symbol not in self.precios_actuales:
            return
        
        precio_actual = self.precios_actuales[symbol]
        necesita_reordenar = False
        cambios = False
        
        for key, tarjeta in list(self.tarjetas_activas.items()):
            if key.startswith(f"{symbol}_"):
                try:
                    entrada = tarjeta['data']['entrada']
                    distancia_pct = abs((entrada - precio_actual) / precio_actual * 100)
                    distancia_anterior = tarjeta['data'].get('distancia_pct', distancia_pct)
                    
                    # Solo actualizar si hay cambio significativo (> 0.01%)
                    if abs(distancia_pct - distancia_anterior) > 0.01:
                        tarjeta['data']['distancia_pct'] = distancia_pct
                        tarjeta['data']['precio_actual'] = precio_actual
                        tarjeta['dist_label'].config(text=f"{distancia_pct:.2f}%")
                        cambios = True
                        
                        # Actualizar color solo si cambió de rango
                        nuevo_color = self.obtener_color_distancia(distancia_pct)
                        color_actual = tarjeta['dist_label'].cget('fg')
                        if nuevo_color != color_actual:
                            tarjeta['dist_label'].config(fg=nuevo_color)
                        
                        if abs(distancia_pct - distancia_anterior) > 0.1:
                            necesita_reordenar = True
                
                except Exception as e:
                    pass
        
        if necesita_reordenar and cambios:
            self.reordenar_tarjetas_suave()
    
    def obtener_color_distancia(self, distancia_pct):
        """Retorna el color según la distancia - evita cálculos repetidos"""
        if distancia_pct < 0.5:
            return "#ff0000"
        elif distancia_pct < 1.0:
            return "#ff8800"
        elif distancia_pct < 2.0:
            return "#ffff00"
        else:
            return "#00ccff"
    
    def reordenar_tarjetas_suave(self):
        """Reordena las tarjetas con animación de deslizamiento OPTIMIZADA"""
        try:
            # Throttling: solo reordenar cada 500ms como máximo
            tiempo_actual = time.time()
            if tiempo_actual - self.ultimo_reorden < 0.5:
                return

            self.ultimo_reorden = tiempo_actual

            # Construir nueva orden
            longs = []
            shorts = []

            for key, tarjeta in self.tarjetas_activas.items():
                tipo = tarjeta['data']['tipo']
                distancia = tarjeta['data']['distancia_pct']

                if tipo == 'LONG':
                    longs.append((distancia, key, tarjeta))
                else:
                    shorts.append((distancia, key, tarjeta))

            longs_ordenados = sorted(longs, key=lambda x: x[0])
            shorts_ordenados = sorted(shorts, key=lambda x: x[0])

            # Animar LONGS con deslizamiento
            y_offset = 0
            for idx, (dist, key, tarjeta) in enumerate(longs_ordenados):
                self.animar_tarjeta_a_posicion(tarjeta['frame'], y_offset)
                y_offset += tarjeta['frame'].winfo_reqheight() + 8

            # Animar SHORTS con deslizamiento
            y_offset = 0
            for idx, (dist, key, tarjeta) in enumerate(shorts_ordenados):
                self.animar_tarjeta_a_posicion(tarjeta['frame'], y_offset)
                y_offset += tarjeta['frame'].winfo_reqheight() + 8

        except Exception as e:
            print(f"Error reordenando tarjetas: {e}")
    
    def animar_tarjeta_a_posicion(self, frame, target_y, duracion=200, pasos=8):
        """Anima un frame a una posición Y objetivo - OPTIMIZADO"""
        try:
            # Si no está usando place(), convertir
            if frame.winfo_manager() != 'place':
                frame.pack_forget()
                frame.place(x=10, y=0, relwidth=0.96)

            y_actual = frame.winfo_y()
            diferencia = target_y - y_actual

            # Si la diferencia es muy pequeña, mover directamente
            if abs(diferencia) < 5:
                frame.place(x=10, y=int(target_y), relwidth=0.96)
                return

            paso_tiempo = duracion // pasos
            paso_distancia = diferencia / pasos

            # Usar easing para suavizar el movimiento
            def animar(paso_actual=0):
                if paso_actual <= pasos:
                    # Easing out cubic para movimiento más natural
                    t = paso_actual / pasos
                    easing = 1 - pow(1 - t, 3)
                    nueva_y = y_actual + (diferencia * easing)
                    frame.place(x=10, y=int(nueva_y), relwidth=0.96)

                    if paso_actual < pasos:
                        self.root.after(paso_tiempo, lambda: animar(paso_actual + 1))

            animar()
        except Exception as e:
            pass  # Silenciar errores para evitar spam en consola

    def recalcular_shock_individual(self, symbol):
        def recalcular():
            print(f"📊 Recalculando order book para {symbol}...")
            
            try:
                order_books = cargar_libro_ordenes_api([symbol], self.base_url)
                
                if symbol not in order_books:
                    print(f"❌ No se pudo obtener order book para {symbol}")
                    return
                
                order_book = order_books[symbol]
                agrupacion = self.agrupaciones[symbol]
                tick = self.tick_sizes[symbol]
                precio_actual = self.precios_actuales[symbol]
                
                shocks_long, shocks_short, decimales_tick = calcular_shocks(
                    order_book, agrupacion, tick)
                
                if len(shocks_long) >= 4:
                    nuevo_shock_long = shocks_long[2]
                    nuevo_stop_long = shocks_long[3]
                    
                    self.shocks_activos[symbol]['long'] = {
                        'entrada': nuevo_shock_long,
                        'stop': nuevo_stop_long
                    }
                    
                    print(f"✅ {symbol} LONG actualizado - Nueva entrada: {nuevo_shock_long}")
                
                if len(shocks_short) >= 4:
                    nuevo_shock_short = shocks_short[2]
                    nuevo_stop_short = shocks_short[3]
                    
                    self.shocks_activos[symbol]['short'] = {
                        'entrada': nuevo_shock_short,
                        'stop': nuevo_stop_short
                    }
                    
                    print(f"✅ {symbol} SHORT actualizado - Nueva entrada: {nuevo_shock_short}")
                
                self.root.after(0, self.reconstruir_ui_desde_shocks)
                
            except Exception as e:
                print(f"Error recalculando shock para {symbol}: {e}")
        
        threading.Thread(target=recalcular, daemon=True).start()
    
    def reconstruir_ui_desde_shocks(self):
        """Reconstruye la UI usando los shocks activos guardados"""
        resultados_long = []
        resultados_short = []
        
        for symbol, shocks in self.shocks_activos.items():
            precio_actual = self.precios_actuales.get(symbol)
            if precio_actual is None:
                continue
            
            tick = self.tick_sizes.get(symbol, 0.01)
            decimales = obtener_decimales_de_tick(tick)
            agrupacion = self.agrupaciones.get(symbol, 0.01)
            
            if 'long' in shocks:
                entrada = shocks['long']['entrada']
                stop = shocks['long']['stop']
                distancia_pct = abs((entrada - precio_actual) / precio_actual * 100)
                
                resultados_long.append({
                    'symbol': symbol,
                    'tipo': 'LONG',
                    'entrada': entrada,
                    'stop_loss': stop,
                    'distancia_pct': distancia_pct,
                    'precio_actual': precio_actual,
                    'decimales': decimales,
                    'agrupacion': agrupacion,
                    'tick_size': tick
                })
            
            if 'short' in shocks:
                entrada = shocks['short']['entrada']
                stop = shocks['short']['stop']
                distancia_pct = abs((entrada - precio_actual) / precio_actual * 100)
                
                resultados_short.append({
                    'symbol': symbol,
                    'tipo': 'SHORT',
                    'entrada': entrada,
                    'stop_loss': stop,
                    'distancia_pct': distancia_pct,
                    'precio_actual': precio_actual,
                    'decimales': decimales,
                    'agrupacion': agrupacion,
                    'tick_size': tick
                })
        
        resultados_long.sort(key=lambda x: x['distancia_pct'])
        resultados_short.sort(key=lambda x: x['distancia_pct'])
        
        self.actualizar_ui(resultados_long, resultados_short)
    
    def actualizar_status(self, mensaje):
        """Actualiza el label de status"""
        try:
            self.root.after(0, lambda: self.lbl_status.config(text=mensaje))
        except:
            pass
    
    def actualizar_ui(self, longs, shorts):
        """Actualiza la interfaz con los resultados"""
        for widget in self.long_container.winfo_children():
            widget.destroy()
        for widget in self.short_container.winfo_children():
            widget.destroy()
        
        self.tarjetas_activas.clear()
        
        total = len(longs) + len(shorts)
        self.lbl_total.config(text=f"Total: {total}")
        self.lbl_longs.config(text=f"Longs: {len(longs)}")
        self.lbl_shorts.config(text=f"Shorts: {len(shorts)}")
        
        for data in longs:
            self.crear_tarjeta_shock(self.long_container, data, "LONG")
        
        for data in shorts:
            self.crear_tarjeta_shock(self.short_container, data, "SHORT")
    
    def cerrar(self):
        """Cierra la aplicación correctamente"""
        self.actualizando = False
        self.root.destroy()

# ---------- EJECUTAR ----------
if __name__ == "__main__":
    root = tk.Tk()
    app = ShockDashboard(root)
    root.protocol("WM_DELETE_WINDOW", app.cerrar)
    root.mainloop()
