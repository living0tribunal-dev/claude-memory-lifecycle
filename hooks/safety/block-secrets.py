#!/usr/bin/env python3
"""
PreToolUse Security Hook - Blockiert Zugriff auf sensitive Dateien
Adaptiert von TheDecipherist/claude-code-mastery

Exit Codes:
  0 = Allow operation
  1 = Error (user notification only)
  2 = BLOCK operation + stderr sent to Claude (Claude learns why!)
"""

import sys
import json
import os

# ============================================================================
# KONFIGURATION: Sensitive Dateien
# ============================================================================

SENSITIVE_FILENAMES = {
    # Environment & Secrets
    '.env', '.env.local', '.env.development', '.env.production', '.env.test',
    'secrets.json', 'secrets.yaml', 'secrets.yml',
    '.secrets', 'credentials.json', 'credentials.yaml',

    # SSH & Keys
    'id_rsa', 'id_rsa.pub', 'id_ed25519', 'id_ed25519.pub',
    'id_dsa', 'id_ecdsa', 'known_hosts', 'authorized_keys',

    # Package Manager Auth
    '.npmrc', '.pypirc', '.gem/credentials',

    # Cloud Credentials
    '.aws/credentials', '.aws/config',
    'gcloud.json', 'service-account.json',
    '.azure/credentials',

    # Database
    '.pgpass', '.my.cnf', '.mongorc.js',

    # Docker
    '.docker/config.json',

    # Kubernetes
    'kubeconfig', '.kube/config',
}

SENSITIVE_EXTENSIONS = {
    '.pem', '.key', '.p12', '.pfx', '.jks', '.keystore',
    '.crt', '.cer', '.der',
}

SENSITIVE_PATTERNS = [
    'secret', 'credential', 'private_key', 'privatekey',
    'password', 'passwd', 'api_key', 'apikey',
    '/secrets/', '/.secrets/',
    'token', 'auth',
]

# Whitelist - diese Dateien sind OK
WHITELIST = {
    '.env.example', '.env.template', '.env.sample',
    'secrets.example.json', 'credentials.example.json',
}

# ============================================================================
# LOGIK
# ============================================================================

def is_sensitive_file(filepath: str) -> tuple[bool, str]:
    """
    Prüft ob eine Datei sensitiv ist.
    Returns: (is_sensitive, reason)
    """
    if not filepath:
        return False, ""

    # Normalisieren
    filepath_lower = filepath.lower().replace('\\', '/')
    filename = os.path.basename(filepath_lower)

    # Whitelist prüfen
    if filename in WHITELIST:
        return False, ""

    # Check 1: Exakte Dateinamen
    if filename in SENSITIVE_FILENAMES:
        return True, f"Dateiname '{filename}' ist in der Sensitive-Liste"

    # Check 2: Dateierweiterungen
    _, ext = os.path.splitext(filename)
    if ext in SENSITIVE_EXTENSIONS:
        return True, f"Dateierweiterung '{ext}' ist sensitiv (Schlüssel/Zertifikat)"

    # Check 3: Pfad-Muster
    for pattern in SENSITIVE_PATTERNS:
        if pattern in filepath_lower:
            return True, f"Pfad enthält sensitives Muster '{pattern}'"

    return False, ""

def main():
    """Hauptfunktion - liest stdin JSON und prüft Dateipfade."""
    try:
        # Input von Claude Code lesen
        input_data = sys.stdin.read()

        if not input_data.strip():
            # Kein Input = durchlassen
            sys.exit(0)

        data = json.loads(input_data)

        # Tool und Parameter extrahieren (unterstützt tool_name UND toolName)
        tool_name = data.get('tool_name') or data.get('toolName', '')
        tool_input = data.get('tool_input') or data.get('toolInput', {})

        # Dateipfad je nach Tool extrahieren
        filepath = None
        if tool_name in ('Read', 'Edit', 'Write'):
            filepath = tool_input.get('file_path', '')
        elif tool_name == 'Bash':
            # Bei Bash: Kommando auf Dateipfade prüfen
            command = tool_input.get('command', '')
            # Einfache Heuristik: Prüfe ob sensitive Dateinamen im Kommando
            for sensitive in SENSITIVE_FILENAMES:
                if sensitive in command.lower():
                    print(f"BLOCKED: Bash-Kommando referenziert sensitive Datei '{sensitive}'", file=sys.stderr)
                    print(f"Grund: Diese Datei könnte Secrets enthalten.", file=sys.stderr)
                    print(f"Lösung: Verwende .env.example als Template oder frage den User.", file=sys.stderr)
                    sys.exit(2)
            sys.exit(0)

        if not filepath:
            sys.exit(0)

        # Prüfen
        is_sensitive, reason = is_sensitive_file(filepath)

        if is_sensitive:
            # EXIT CODE 2: Block + Feedback an Claude
            print(f"BLOCKED: Zugriff auf '{filepath}' verweigert", file=sys.stderr)
            print(f"Grund: {reason}", file=sys.stderr)
            print(f"Dies ist eine Sicherheitsmaßnahme um Secrets zu schützen.", file=sys.stderr)
            print(f"Lösung: Falls du den Inhalt wirklich brauchst, frage den User.", file=sys.stderr)
            sys.exit(2)

        # Alles OK
        sys.exit(0)

    except json.JSONDecodeError:
        # Ungültiges JSON = durchlassen (fail open)
        sys.exit(0)
    except Exception as e:
        # Unerwarteter Fehler = durchlassen (fail open)
        print(f"block-secrets.py Warnung: {e}", file=sys.stderr)
        sys.exit(0)

if __name__ == '__main__':
    main()
