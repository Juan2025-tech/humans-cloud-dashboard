"""
auth.py - Módulo de autenticación para HumanS
=============================================
Gestiona la autenticación de usuarios mediante archivo CSV.

USO:
    from auth import validate_user, load_users, init_auth_routes

FORMATO CSV (data/users.csv):
    username,password,role,name
    admin,admin123,admin,Administrador
    medico1,pass123,medico,Dr. García
"""

import os
import csv
import hashlib
import secrets
from functools import wraps
from flask import session, redirect, url_for, request, jsonify

# Configuración
USERS_CSV_PATH = os.path.join(os.path.dirname(__file__), 'data', 'users.csv')
SESSION_SECRET_KEY = os.environ.get('SESSION_SECRET', secrets.token_hex(16))

# Cache de usuarios (se recarga al modificar el CSV)
_users_cache = {}
_csv_mtime = 0


def load_users(force_reload=False):
    """
    Carga usuarios desde el archivo CSV.
    Usa caché para evitar lecturas repetidas.
    
    Returns:
        dict: {username: {password, role, name}}
    """
    global _users_cache, _csv_mtime
    
    if not os.path.exists(USERS_CSV_PATH):
        print(f"⚠️ Archivo de usuarios no encontrado: {USERS_CSV_PATH}")
        return {}
    
    # Verificar si el archivo cambió
    current_mtime = os.path.getmtime(USERS_CSV_PATH)
    if not force_reload and _users_cache and current_mtime == _csv_mtime:
        return _users_cache
    
    try:
        _users_cache = {}
        with open(USERS_CSV_PATH, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                username = row.get('username', '').strip().lower()
                if username:
                    _users_cache[username] = {
                        'password': row.get('password', ''),
                        'role': row.get('role', 'user'),
                        'name': row.get('name', username)
                    }
        
        _csv_mtime = current_mtime
        print(f"✅ Usuarios cargados: {len(_users_cache)}")
        return _users_cache
    
    except Exception as e:
        print(f"❌ Error cargando usuarios: {e}")
        return {}


def validate_user(username, password):
    """
    Valida las credenciales de un usuario.
    
    Args:
        username: Nombre de usuario
        password: Contraseña en texto plano
    
    Returns:
        dict|None: Datos del usuario si es válido, None si no
    """
    users = load_users()
    username = username.strip().lower()
    
    if username not in users:
        return None
    
    user = users[username]
    
    # Comparación simple (para producción usar hash)
    if user['password'] == password:
        return {
            'username': username,
            'role': user['role'],
            'name': user['name']
        }
    
    return None


def hash_password(password):
    """
    Genera hash SHA-256 de una contraseña.
    Para uso futuro con contraseñas hasheadas.
    """
    return hashlib.sha256(password.encode()).hexdigest()


def login_required(f):
    """
    Decorador para proteger rutas que requieren autenticación.
    
    Uso:
        @app.route('/dashboard')
        @login_required
        def dashboard():
            return render_template('index11.html')
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            # Si es una petición API, devolver JSON
            if request.path.startswith('/api/'):
                return jsonify({'error': 'No autorizado'}), 401
            # Si no, redirigir al login
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function


def role_required(roles):
    """
    Decorador para verificar rol del usuario.
    
    Uso:
        @app.route('/admin')
        @login_required
        @role_required(['admin'])
        def admin_panel():
            ...
    """
    if isinstance(roles, str):
        roles = [roles]
    
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                return redirect(url_for('login_page'))
            
            user_role = session['user'].get('role', '')
            if user_role not in roles:
                return jsonify({'error': 'Acceso denegado'}), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def get_current_user():
    """
    Obtiene el usuario actual de la sesión.
    
    Returns:
        dict|None: Datos del usuario o None
    """
    return session.get('user')


def init_auth_routes(app):
    """
    Inicializa las rutas de autenticación en la aplicación Flask.
    
    Rutas añadidas:
        GET  /login     - Página de login
        POST /api/login - API de autenticación
        GET  /logout    - Cerrar sesión
    
    Uso:
        from auth import init_auth_routes
        init_auth_routes(app)
    """
    from flask import render_template
    
    @app.route('/login')
    def login_page():
        # Si ya está logueado, ir al dashboard
        if 'user' in session:
            return redirect(url_for('dashboard'))
        return render_template('login.html')
    
    @app.route('/api/login', methods=['POST'])
    def api_login():
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'Datos no proporcionados'}), 400
        
        username = data.get('username', '')
        password = data.get('password', '')
        
        if not username or not password:
            return jsonify({'error': 'Usuario y contraseña requeridos'}), 400
        
        user = validate_user(username, password)
        
        if user:
            session['user'] = user
            session.permanent = True  # Cookie persistente
            print(f"✅ Login exitoso: {username}")
            return jsonify({
                'success': True,
                'user': {
                    'username': user['username'],
                    'name': user['name'],
                    'role': user['role']
                }
            })
        
        print(f"❌ Login fallido: {username}")
        return jsonify({'error': 'Usuario o contraseña incorrectos'}), 401
    
    @app.route('/logout')
    def logout():
        username = session.get('user', {}).get('username', 'unknown')
        session.clear()
        print(f"🚪 Logout: {username}")
        return redirect(url_for('login_page'))
    
    @app.route('/api/user')
    @login_required
    def api_current_user():
        """Devuelve datos del usuario actual."""
        return jsonify(session.get('user'))
    
    print("✅ Rutas de autenticación inicializadas")


# Crear directorio y archivo CSV de ejemplo si no existen
def ensure_users_file():
    """
    Crea el archivo de usuarios por defecto si no existe.
    """
    data_dir = os.path.dirname(USERS_CSV_PATH)
    
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
        print(f"📁 Directorio creado: {data_dir}")
    
    if not os.path.exists(USERS_CSV_PATH):
        default_users = [
            ['username', 'password', 'role', 'name'],
            ['admin', 'admin123', 'admin', 'Administrador'],
            ['medico', 'medico123', 'medico', 'Dr. García'],
            ['enfermero', 'enf123', 'enfermero', 'Enfermero López'],
        ]
        
        with open(USERS_CSV_PATH, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(default_users)
        
        print(f"✅ Archivo de usuarios creado: {USERS_CSV_PATH}")
        print("⚠️  IMPORTANTE: Cambia las contraseñas por defecto")


# Auto-crear archivo al importar el módulo
ensure_users_file()
