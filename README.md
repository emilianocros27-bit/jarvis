# Jarvis

Aplicación web personal para explorar notas, proyectos y memoria en una interfaz tipo "knowledge galaxy".

## Ejecutar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Abre `http://localhost:4700`.

## Despliegue

El servidor detecta automáticamente la variable `PORT` que proporcionan Render, Railway y hosts equivalentes. Configura:

- Build command: `pip install -r requirements.txt`
- Start command: `python server.py`

No subas `credentials/` ni `token.json`: están ignorados intencionalmente. Las integraciones de Google y Firebase requieren que sus credenciales se configuren de forma segura en el entorno del host. La función de chat también invoca la CLI local de Claude (`claude -p`), por lo que no funcionará en un hosting estándar hasta que se sustituya por una integración de servidor compatible.
