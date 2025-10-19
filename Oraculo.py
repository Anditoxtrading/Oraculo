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

# ---------- FUNCIONES DE ARCHIVO ----------

RUTA_ARCHIVO = "agrupaciones.txt"

def cargar_agrupaciones_guardadas():
    if os.path.exists(RUTA_ARCHIVO):
        try:
            with open(RUTA_ARCHIVO, "r") as f:
                return json.load(f)
        except Exception as e:
            pass
    return {}

def guardar_agrupaciones(agrupaciones):
    try:
        with open(RUTA_ARCHIVO, "w") as f:
            json.dump(agrupaciones, f, indent=4)
    except Exception as e:
        pass

# ---------- OBTENER DATOS BINANCE ----------

def obtener_precio_actual(symbol):
    url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
    try:
        return float(requests.get(url, timeout=5).json()['price'])
    except Exception as e:
        return None

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
        self.agrupaciones = cargar_agrupaciones_guardadas()
        self.tick_sizes = {}
        self.base_url = "http://localhost:8000"
        self.tarjetas_activas = {}  # {symbol_tipo: {widget, data}}
        self.precios_actuales = {}  # Cache de precios
        self.shocks_activos = {}    # {symbol: {long: {entrada, stop}, short: {entrada, stop}}}
        self.actualizando = True
        self.precio_anterior = {}   # Para detectar cruces
        self.hilos_monitores = {}   # {symbol: thread} - un hilo por moneda
        self.animando = False       # Flag para evitar animaciones simultáneas
        self.cola_reordenamiento = []  # Cola de reordenamientos pendientes
        
        # Crear interfaz
        self.crear_interfaz()
        
        # Iniciar escaneo inicial
        self.escaneo_inicial()
    
    def crear_interfaz(self):
        # Header
        header = tk.Frame(self.root, bg="#1a1a2e", height=100)
        header.pack(fill=tk.X, padx=10, pady=10)
        
        titulo = tk.Label(header, text="📊 ANÁLISIS DE SHOCKS", 
                         font=("Segoe UI", 28, "bold"), 
                         bg="#1a1a2e", fg="#00ff88")
        titulo.pack(pady=10)
        
        # Stats
        stats_frame = tk.Frame(header, bg="#1a1a2e")
        stats_frame.pack()
        
        self.lbl_total = tk.Label(stats_frame, text="Total: 0", 
                                  font=("Segoe UI", 12), 
                                  bg="#1a1a2e", fg="#ffffff")
        self.lbl_total.pack(side=tk.LEFT, padx=20)
        
        self.lbl_longs = tk.Label(stats_frame, text="Longs: 0", 
                                  font=("Segoe UI", 12), 
                                  bg="#1a1a2e", fg="#00ff88")
        self.lbl_longs.pack(side=tk.LEFT, padx=20)
        
        self.lbl_shorts = tk.Label(stats_frame, text="Shorts: 0", 
                                   font=("Segoe UI", 12), 
                                   bg="#1a1a2e", fg="#ff4444")
        self.lbl_shorts.pack(side=tk.LEFT, padx=20)
        
        self.lbl_status = tk.Label(stats_frame, text="🟢 Monitoreando", 
                                   font=("Segoe UI", 11), 
                                   bg="#1a1a2e", fg="#00ff88")
        self.lbl_status.pack(side=tk.LEFT, padx=20)
        
        # Contenedor de columnas
        columnas = tk.Frame(self.root, bg="#0f0f1e")
        columnas.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Columna LONG (Verde)
        long_frame = tk.Frame(columnas, bg="#0f0f1e")
        long_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        long_header = tk.Label(long_frame, text="🟢 LONG POSITIONS", 
                               font=("Segoe UI", 18, "bold"),
                               bg="#00cc66", fg="#ffffff", 
                               pady=15)
        long_header.pack(fill=tk.X)
        
        # Scroll para longs
        long_scroll_frame = tk.Frame(long_frame, bg="#1a1a2e")
        long_scroll_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        long_canvas = tk.Canvas(long_scroll_frame, bg="#1a1a2e", highlightthickness=0)
        long_scrollbar = ttk.Scrollbar(long_scroll_frame, orient="vertical", command=long_canvas.yview)
        self.long_container = tk.Frame(long_canvas, bg="#1a1a2e")
        
        self.long_container.bind("<Configure>", 
                                lambda e: long_canvas.configure(scrollregion=long_canvas.bbox("all")))
        
        long_canvas.create_window((0, 0), window=self.long_container, anchor="nw")
        long_canvas.configure(yscrollcommand=long_scrollbar.set)
        
        long_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        long_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Columna SHORT (Rojo)
        short_frame = tk.Frame(columnas, bg="#0f0f1e")
        short_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        short_header = tk.Label(short_frame, text="🔴 SHORT POSITIONS", 
                                font=("Segoe UI", 18, "bold"),
                                bg="#cc0000", fg="#ffffff", 
                                pady=15)
        short_header.pack(fill=tk.X)
        
        # Scroll para shorts
        short_scroll_frame = tk.Frame(short_frame, bg="#1a1a2e")
        short_scroll_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        short_canvas = tk.Canvas(short_scroll_frame, bg="#1a1a2e", highlightthickness=0)
        short_scrollbar = ttk.Scrollbar(short_scroll_frame, orient="vertical", command=short_canvas.yview)
        self.short_container = tk.Frame(short_canvas, bg="#1a1a2e")
        
        self.short_container.bind("<Configure>", 
                                 lambda e: short_canvas.configure(scrollregion=short_canvas.bbox("all")))
        
        short_canvas.create_window((0, 0), window=self.short_container, anchor="nw")
        short_canvas.configure(yscrollcommand=short_scrollbar.set)
        
        short_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        short_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    
    def crear_tarjeta_shock(self, container, data, tipo):
        symbol = data['symbol']
        key = f"{symbol}_{tipo}"
        
        # Frame principal de la tarjeta con efecto de aparición
        card = tk.Frame(container, bg="#16213e", relief=tk.RAISED, bd=2)
        card.pack(fill=tk.X, padx=10, pady=8)
        
        color = "#00ff88" if tipo == "LONG" else "#ff4444"
        
        # TODO EN UNA LÍNEA
        info_frame = tk.Frame(card, bg="#16213e")
        info_frame.pack(fill=tk.X, padx=20, pady=15)
        
        formato = f".{data['decimales']}f"
        
        # Moneda
        tk.Label(info_frame, text="Moneda:", 
                font=("Segoe UI", 9, "bold"),
                bg="#16213e", fg="#888888").pack(side=tk.LEFT, padx=(0, 5))
        
        # Label simple para símbolo (más rápido que Entry)
        symbol_label = tk.Label(info_frame, text=symbol,
                               font=("Segoe UI", 11, "bold"),
                               bg="#16213e", fg=color,
                               cursor="hand2")
        symbol_label.pack(side=tk.LEFT, padx=(0, 20))
        
        # Copiar al hacer click
        symbol_label.bind("<Button-1>", lambda e: self.copiar_al_portapapeles(symbol))
        
        # Shock (Entrada)
        tk.Label(info_frame, text="Shock:", 
                font=("Segoe UI", 9, "bold"),
                bg="#16213e", fg="#888888").pack(side=tk.LEFT, padx=(0, 5))
        
        # Label para precio de entrada
        entrada_str = f"${data['entrada']:{formato}}"
        entrada_valor = f"{data['entrada']:{formato}}"  # Sin el símbolo $
        entrada_label = tk.Label(info_frame, text=entrada_str,
                                font=("Courier New", 10, "bold"),
                                bg="#16213e", fg=color,
                                cursor="hand2")
        entrada_label.pack(side=tk.LEFT, padx=(0, 20))
        entrada_label.bind("<Button-1>", lambda e: self.copiar_al_portapapeles(entrada_valor))
        
        # Stop Loss
        tk.Label(info_frame, text="Stop:", 
                font=("Segoe UI", 9, "bold"),
                bg="#16213e", fg="#888888").pack(side=tk.LEFT, padx=(0, 5))
        
        # Label para stop loss
        stop_str = f"${data['stop_loss']:{formato}}"
        stop_valor = f"{data['stop_loss']:{formato}}"  # Sin el símbolo $
        stop_label = tk.Label(info_frame, text=stop_str,
                             font=("Courier New", 10, "bold"),
                             bg="#16213e", fg="#ffa500",
                             cursor="hand2")
        stop_label.pack(side=tk.LEFT, padx=(0, 5))
        stop_label.bind("<Button-1>", lambda e: self.copiar_al_portapapeles(stop_valor))
        
        # Calcular distancia FIJA entre entrada y stop loss (solo una vez)
        entrada = data['entrada']
        stop_loss = data['stop_loss']
        distancia_entrada_stop_pct = abs((stop_loss - entrada) / entrada * 100)
        
        # Label para distancia fija entrada-stop (en negrita, sin paréntesis)
        stop_dist_label = tk.Label(info_frame, text=f"{distancia_entrada_stop_pct:.2f}%",
                                   font=("Courier New", 10, "bold"),
                                   bg="#16213e", fg="#ff6b6b")
        stop_dist_label.pack(side=tk.LEFT, padx=(0, 20))
        
        # Distancia (se actualizará en tiempo real)
        tk.Label(info_frame, text="Dist:", 
                font=("Segoe UI", 9, "bold"),
                bg="#16213e", fg="#888888").pack(side=tk.LEFT, padx=(0, 5))
        
        dist_label = tk.Label(info_frame, text=f"{data['distancia_pct']:.2f}%", 
                             font=("Courier New", 10, "bold"),
                             bg="#16213e", fg="#00ccff")
        dist_label.pack(side=tk.LEFT)
        
        # Guardar referencia para actualización en tiempo real
        self.tarjetas_activas[key] = {
            'frame': card,
            'dist_label': dist_label,
            'symbol_label': symbol_label,
            'entrada_label': entrada_label,
            'stop_label': stop_label,
            'data': data,
            'color': color
        }
        
        return card
    
    def copiar_al_portapapeles(self, texto):
        """Copia texto al portapapeles"""
        self.root.clipboard_clear()
        self.root.clipboard_append(texto)
        # Feedback visual rápido
        self.actualizar_status(f"📋 Copiado: {texto}")
    
    def escaneo_inicial(self):
        """Escaneo inicial para obtener todos los shocks (solo una vez al iniciar)"""
        def escanear():
            print("🔍 Realizando escaneo inicial de order books...")
            self.actualizar_status("🔍 Escaneando...")
            
            symbols = obtener_simbolos(self.base_url)
            if not symbols:
                print("❌ No hay símbolos disponibles")
                self.root.after(10000, self.escaneo_inicial)
                return
            
            # Cargar tick sizes
            for sym in symbols:
                if sym not in self.tick_sizes:
                    self.tick_sizes[sym] = obtener_tick_size(sym)
                    precio = obtener_precio_actual(sym)
                    if precio:
                        agrupacion_optima = obtener_nivel_agrupacion_optimo(self.tick_sizes[sym], precio)
                        self.agrupaciones[sym] = agrupacion_optima
                        guardar_agrupaciones(self.agrupaciones)
            
            # Obtener TODOS los libros de órdenes (solo en el escaneo inicial)
            order_books = cargar_libro_ordenes_api(symbols, self.base_url)
            if not order_books:
                print("❌ No hay datos de libros de órdenes")
                self.root.after(10000, self.escaneo_inicial)
                return
            
            # Procesar datos y guardar shocks
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
                
                # Guardar shocks para monitoreo
                if symbol not in self.shocks_activos:
                    self.shocks_activos[symbol] = {}
                
                if len(shocks_long) >= 4:
                    shock_1_long = shocks_long[2]
                    shock_2_long = shocks_long[3]
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
                
                if len(shocks_short) >= 4:
                    shock_1_short = shocks_short[2]
                    shock_2_short = shocks_short[3]
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
            
            # Ordenar por distancia
            resultados_long.sort(key=lambda x: x['distancia_pct'])
            resultados_short.sort(key=lambda x: x['distancia_pct'])
            
            print(f"✅ Escaneo inicial completado: {len(resultados_long)} LONGs, {len(resultados_short)} SHORTs")
            
            # Actualizar UI
            self.root.after(0, lambda: self.actualizar_ui(resultados_long, resultados_short))
            self.actualizar_status("🟢 Monitoreando")
            
            # Iniciar un hilo para cada moneda
            self.root.after(0, self.iniciar_hilos_monitores)
        
        threading.Thread(target=escanear, daemon=True).start()
    
    def iniciar_hilos_monitores(self):
        """Inicia un hilo de monitoreo para cada moneda"""
        for symbol in self.shocks_activos.keys():
            if symbol not in self.hilos_monitores:
                hilo = threading.Thread(target=self.monitorear_moneda, args=(symbol,), daemon=True)
                hilo.start()
                self.hilos_monitores[symbol] = hilo
                print(f"🔍 Hilo de monitoreo iniciado para {symbol}")
    
    def monitorear_moneda(self, symbol):
        """Monitorea una moneda específica en su propio hilo"""
        print(f"▶️ Iniciando monitoreo de {symbol}")
        
        while self.actualizando:
            try:
                # Obtener precio actual
                precio_actual = obtener_precio_actual(symbol)
                
                if precio_actual is None:
                    time.sleep(0.5)
                    continue
                
                precio_prev = self.precio_anterior.get(symbol, precio_actual)
                self.precios_actuales[symbol] = precio_actual
                
                # Detectar toque para LONG
                if 'long' in self.shocks_activos.get(symbol, {}):
                    entrada_long = self.shocks_activos[symbol]['long']['entrada']
                    
                    # Detectar cruce: precio bajó y tocó/cruzó la entrada
                    if precio_prev > entrada_long and precio_actual <= entrada_long:
                        print(f"🎯 TOQUE LONG detectado en {symbol} - Precio: {precio_actual}, Entrada: {entrada_long}")
                        self.actualizar_status(f"🎯 TOQUE LONG: {symbol}")
                        # Recalcular order book para esta moneda
                        self.recalcular_shock_individual(symbol)
                
                # Detectar toque para SHORT
                if 'short' in self.shocks_activos.get(symbol, {}):
                    entrada_short = self.shocks_activos[symbol]['short']['entrada']
                    
                    # Detectar cruce: precio subió y tocó/cruzó la entrada
                    if precio_prev < entrada_short and precio_actual >= entrada_short:
                        print(f"🎯 TOQUE SHORT detectado en {symbol} - Precio: {precio_actual}, Entrada: {entrada_short}")
                        self.actualizar_status(f"🎯 TOQUE SHORT: {symbol}")
                        # Recalcular order book para esta moneda
                        self.recalcular_shock_individual(symbol)
                
                self.precio_anterior[symbol] = precio_actual
                
                # Actualizar distancias en UI (solo para esta moneda)
                self.root.after(0, lambda s=symbol: self.actualizar_distancia_moneda(s))
                
            except Exception as e:
                print(f"Error monitoreando {symbol}: {e}")
            
            time.sleep(2)  # Monitorear cada 2 segundos
        
        print(f"⏹️ Monitoreo detenido para {symbol}")
    
    def actualizar_distancia_moneda(self, symbol):
        """Actualiza la distancia solo para una moneda específica y reordena si es necesario"""
        necesita_reordenar = False
        
        for key, tarjeta in list(self.tarjetas_activas.items()):
            if key.startswith(f"{symbol}_"):
                try:
                    if symbol in self.precios_actuales:
                        precio_actual = self.precios_actuales[symbol]
                        entrada = tarjeta['data']['entrada']
                        
                        # Calcular nueva distancia a la entrada
                        distancia_pct = abs((entrada - precio_actual) / precio_actual * 100)
                        
                        # Guardar distancia anterior para detectar cambios significativos
                        distancia_anterior = tarjeta['data'].get('distancia_pct', distancia_pct)
                        
                        # Actualizar data
                        tarjeta['data']['distancia_pct'] = distancia_pct
                        tarjeta['data']['precio_actual'] = precio_actual
                        
                        # Actualizar label de distancia a entrada
                        tarjeta['dist_label'].config(text=f"{distancia_pct:.2f}%")
                        
                        # Cambiar color según proximidad (entrada)
                        if distancia_pct < 0.5:
                            tarjeta['dist_label'].config(fg="#ff0000")
                        elif distancia_pct < 1.0:
                            tarjeta['dist_label'].config(fg="#ff8800")
                        elif distancia_pct < 2.0:
                            tarjeta['dist_label'].config(fg="#ffff00")
                        else:
                            tarjeta['dist_label'].config(fg="#00ccff")
                        
                        # Detectar si hay cambio significativo (más de 0.1% de diferencia)
                        if abs(distancia_pct - distancia_anterior) > 0.1:
                            necesita_reordenar = True
                
                except Exception as e:
                    pass
        
        # Si hubo cambio significativo, reordenar dinámicamente
        if necesita_reordenar:
            self.reordenar_tarjetas_dinamico()
    
    def reordenar_tarjetas_dinamico(self):
        """Reordena las tarjetas de forma suave y sin parpadeos"""
        if self.animando:
            return
        
        self.animando = True
        
        try:
            # Separar longs y shorts con sus datos actualizados
            longs = []
            shorts = []
            
            for key, tarjeta in self.tarjetas_activas.items():
                tipo = tarjeta['data']['tipo']
                distancia = tarjeta['data']['distancia_pct']
                
                if tipo == 'LONG':
                    longs.append((distancia, key, tarjeta))
                else:
                    shorts.append((distancia, key, tarjeta))
            
            # Ordenar por distancia (menor a mayor)
            longs_nuevos = sorted(longs, key=lambda x: x[0])
            shorts_nuevos = sorted(shorts, key=lambda x: x[0])
            
            # Reordenar LONGS de forma limpia
            for idx, (dist, key, tarjeta) in enumerate(longs_nuevos):
                tarjeta['frame'].pack_forget()
            
            for idx, (dist, key, tarjeta) in enumerate(longs_nuevos):
                tarjeta['frame'].pack(fill=tk.X, padx=10, pady=8)
            
            # Reordenar SHORTS de forma limpia
            for idx, (dist, key, tarjeta) in enumerate(shorts_nuevos):
                tarjeta['frame'].pack_forget()
            
            for idx, (dist, key, tarjeta) in enumerate(shorts_nuevos):
                tarjeta['frame'].pack(fill=tk.X, padx=10, pady=8)
            
            # Liberar flag después de un pequeño delay
            self.root.after(200, self.liberar_animacion)
        
        except Exception as e:
            print(f"Error reordenando tarjetas: {e}")
            self.animando = False
    
    def liberar_animacion(self):
        """Libera el flag de animación"""
        self.animando = False
    
    def recalcular_shock_individual(self, symbol):
        """Recalcula el shock para una moneda específica cuando toca el precio de entrada"""
        def recalcular():
            print(f"📊 Recalculando order book para {symbol}...")
            
            try:
                # Obtener order book solo para esta moneda
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
                
                # Actualizar shocks activos
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
                
                # Reconstruir UI con datos actualizados
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
        # Limpiar contenedores y referencias
        for widget in self.long_container.winfo_children():
            widget.destroy()
        for widget in self.short_container.winfo_children():
            widget.destroy()
        
        self.tarjetas_activas.clear()
        
        # Actualizar stats
        total = len(longs) + len(shorts)
        self.lbl_total.config(text=f"Total: {total}")
        self.lbl_longs.config(text=f"Longs: {len(longs)}")
        self.lbl_shorts.config(text=f"Shorts: {len(shorts)}")
        
        # Crear tarjetas
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