"""Machine-bound Keygen activation. Only a signed machine certificate grants access."""
import base64
import hashlib
import json
import math
import os
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib import request, error, parse

import certifi
import keyring
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import license_config as config

SERVICE = 'com.salikha.photoman.license'
REFRESH_INTERVAL = 86400
NETWORK_RETRY_INTERVAL = 900
ACTIVATION_REQUIRED = {'NO_MACHINE', 'NO_MACHINES', 'FINGERPRINT_SCOPE_MISMATCH', 'MACHINE_SCOPE_REQUIRED'}
INVALID_LICENSE_CODES = {'SUSPENDED', 'EXPIRED', 'OVERDUE', 'BANNED', 'NOT_FOUND',
                         'NO_MACHINE', 'NO_MACHINES', 'FINGERPRINT_SCOPE_MISMATCH',
                         'TOO_MANY_MACHINES', 'MACHINE_SCOPE_MISMATCH', 'HEARTBEAT_DEAD',
                         'PRODUCT_SCOPE_MISMATCH', 'POLICY_SCOPE_MISMATCH'}

class LicenseError(Exception):
    pass

class NetworkError(LicenseError):
    pass

class LicenseRejected(LicenseError):
    pass

class ApiError(LicenseError):
    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status, self.code = status, code

def relation(resource, name):
    return (resource.get('relationships', {}).get(name, {}).get('data') or {}).get('id')

def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Missing timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Missing timestamp timezone')
    return parsed.timestamp()

def device_fingerprint():
    """Stable, namespaced hardware identity; no hostname or MAC address."""
    if sys.platform != 'win32':
        raise LicenseError('This Windows release supports Windows only.')
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                        0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
        install_id = winreg.QueryValueEx(handle, 'MachineGuid')[0]
    try:
        hardware = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                   '(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID'],
                                  capture_output=True, text=True, check=True, timeout=20,
                                  stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        hardware = ''
    # Some Windows installations deny WMI access to standard users. MachineGuid
    # is still a stable per-installation identifier, so keep licensing usable
    # while retaining the namespaced hash and never sending the raw value.
    if not hardware or hardware.replace('-', '').lower() in ('0' * 32, 'f' * 32):
        hardware = 'machineguid-only'
    return hashlib.sha256(('salikha-photoman:v1:' + (install_id + ':' + hardware).strip().lower()).encode()).hexdigest()

def verify_machine_file(certificate, fingerprint, now=None):
    now = time.time() if now is None else now
    try:
        lines = certificate.strip().splitlines()
        if lines[0] != '-----BEGIN MACHINE FILE-----' or lines[-1] != '-----END MACHINE FILE-----':
            raise ValueError('Not a machine file')
        envelope = json.loads(base64.b64decode(''.join(lines[1:-1]), validate=True))
        if envelope['alg'] != 'base64+ed25519':
            raise ValueError('Unsupported signing algorithm')
        encoded = envelope['enc']
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(config.PUBLIC_KEY_HEX)).verify(
            base64.b64decode(envelope['sig'], validate=True), ('machine/' + encoded).encode())
        payload = json.loads(base64.b64decode(encoded, validate=True))
        issued, expiry = timestamp(payload['meta']['issued']), timestamp(payload['meta']['expiry'])
        if issued > now + 300 or expiry <= now or expiry <= issued or expiry - issued > config.OFFLINE_SECONDS + 300:
            raise ValueError('Expired permission or incorrect system clock')
        machine = payload['data']
        if machine['type'] != 'machines' or machine['attributes']['fingerprint'] != fingerprint:
            raise ValueError('Permission belongs to a different computer')
        if relation(machine, 'account') != config.ACCOUNT_ID or relation(machine, 'product') != config.PRODUCT_ID:
            raise ValueError('Permission belongs to another product')
        license_id = relation(machine, 'license')
        if not machine.get('id') or not license_id:
            raise ValueError('Missing machine or license identity')
        license_data = next(item for item in payload['included'] if item['type'] == 'licenses' and item['id'] == license_id)
        if relation(license_data, 'policy') != config.POLICY_ID:
            raise ValueError('Unexpected license policy')
        attributes = license_data['attributes']
        if attributes.get('suspended') or attributes.get('status') not in ('ACTIVE', 'INACTIVE', 'EXPIRING'):
            raise ValueError('License status does not allow use')
        if attributes.get('expiry') and timestamp(attributes['expiry']) <= now:
            raise ValueError('License has expired')
        return {'machine_id': machine['id'], 'license_id': license_id, 'issued': issued,
                'expiry': min(expiry, timestamp(attributes['expiry'])) if attributes.get('expiry') else expiry}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, StopIteration,
            OverflowError, OSError, InvalidSignature) as exc:
        raise LicenseError('Invalid, expired, or modified machine permission. Activate or refresh your license.') from exc

class LicenseManager:
    def __init__(self, directory, fingerprint=None):
        self.directory = Path(directory); self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'machine.lic'; self.clock_path = self.directory / 'license-clock.json'
        try: self.fingerprint = fingerprint or device_fingerprint()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise LicenseError('Cannot read this computer identity. Contact Salikha support.') from exc
        self.lock = threading.RLock(); self.clock_wall, self.clock_mono = time.time(), time.monotonic()
        self.next_refresh = 0
        self.refreshing = False
        self.generation = 0
        self.logged_out = False

    def _now(self):
        return max(time.time(), self.clock_wall + time.monotonic() - self.clock_mono)

    def permission(self):
        with self.lock:
            try:
                if self.logged_out: raise LicenseError('You are logged out. Activate your license to continue.')
                now = self._now()
                try:
                    last_seen = float(json.loads(self.clock_path.read_text(encoding='utf-8'))['last_seen'])
                except FileNotFoundError:
                    last_seen = now
                if not math.isfinite(last_seen) or now + 300 < last_seen:
                    raise LicenseError('System clock moved backwards. Correct it and refresh the license.')
                now = max(last_seen, now)
                permission = verify_machine_file(self.path.read_text(encoding='utf-8'), self.fingerprint, now)
                self._atomic_write(self.clock_path, json.dumps({'last_seen': now}))
                return permission
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise LicenseError('This computer needs license activation.') from exc

    def require(self): return self.permission()

    def _atomic_write(self, destination, content):
        temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with open(temporary, 'x', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600); stream.write(content)
            os.replace(temporary, destination)
        finally: temporary.unlink(missing_ok=True)

    def _api(self, method, path, key, body=None):
        url = 'https://api.keygen.sh/v1/accounts/' + config.ACCOUNT_ID + path
        headers = {'Accept': 'application/vnd.api+json', 'Content-Type': 'application/vnd.api+json',
                   'Authorization': 'License ' + key, 'Keygen-Version': '1.8'}
        environment = getattr(config, 'ENVIRONMENT', '')
        if environment:
            headers['Keygen-Environment'] = environment
        req = request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=20, context=ssl.create_default_context(cafile=certifi.where())) as response:
                content = response.read(2_000_001)
                if len(content) > 2_000_000:
                    raise NetworkError('Unexpected licensing response size.')
                document = json.loads(content) if content else {}
                if not isinstance(document, dict):
                    raise NetworkError('Unexpected licensing service response. Try again later.')
                return document
        except error.HTTPError as exc:
            try:
                details = json.loads(exc.read(65536))['errors'][0]; code = details.get('code', ''); detail = details.get('detail', 'License request rejected.')
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                code, detail = '', 'License request rejected.'
            raise ApiError(exc.code, code, str(detail).replace(key, '[redacted]')) from None
        except (error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError('Cannot reach Keygen. Check your internet connection and try again.') from exc
        except ValueError as exc: raise NetworkError('Unexpected licensing service response. Try again later.') from exc

    def _check_license(self, resource):
        if not isinstance(resource, dict) or not resource.get('id'):
            raise NetworkError('Keygen did not return the validated license.')
        if resource.get('type') != 'licenses' or relation(resource, 'product') != config.PRODUCT_ID or relation(resource, 'policy') != config.POLICY_ID:
            raise LicenseError('This key is not for Salikha Photoman.')

    def activate(self, key, remember=False):
        with self.lock:
            self.generation += 1
            generation = self.generation
        return self._activate(key, remember, generation, allow_registration=True)

    def _validate(self, key, scopes):
        response = self._api('POST', '/licenses/actions/validate-key', key,
                             {'meta': {'key': key, 'scope': scopes}})
        meta = response.get('meta')
        if not isinstance(meta, dict) or not isinstance(meta.get('valid'), bool):
            raise NetworkError('Unexpected validation response from Keygen. Try again later.')
        return response.get('data'), meta

    def _reject_validation(self, meta, key):
        code = meta.get('code')
        detail = str(meta.get('detail') or code or 'Unable to validate key.').replace(key, '[redacted]')
        exception = LicenseRejected if code in INVALID_LICENSE_CODES else LicenseError
        raise exception('License rejected: ' + detail)

    def _activate(self, key, remember, generation, *, allow_registration):
        key = key.strip()
        if not key or len(key) > 16000 or any(c.isspace() for c in key): raise LicenseError('Paste a valid license key without spaces or line breaks.')
        scopes = {'fingerprint': self.fingerprint, 'policy': config.POLICY_ID, 'product': config.PRODUCT_ID}
        resource, meta = self._validate(key, scopes)
        machine_path = '/machines/' + self.fingerprint
        machine_id = None
        if not meta['valid']:
            code = meta.get('code')
            if code not in ACTIVATION_REQUIRED:
                self._reject_validation(meta, key)
            self._check_license(resource)
            try:
                machine = self._api('GET', machine_path, key).get('data')
            except ApiError as exc:
                if exc.status != 404:
                    raise
                if not allow_registration:
                    raise LicenseRejected('This computer is no longer activated. Enter your key to activate again.') from exc
                try:
                    machine = self._api('POST', '/machines', key, {'data': {
                        'type': 'machines',
                        'attributes': {'fingerprint': self.fingerprint, 'platform': sys.platform, 'name': 'Salikha Windows'},
                        'relationships': {'license': {'data': {'type': 'licenses', 'id': resource['id']}}},
                    }}).get('data')
                except ApiError as create_error:
                    if create_error.code != 'FINGERPRINT_TAKEN':
                        raise
                    machine = self._api('GET', machine_path, key).get('data')
            if (not isinstance(machine, dict) or machine.get('type') != 'machines'
                    or relation(machine, 'license') != resource['id'] or not machine.get('id')
                    or (machine.get('attributes') or {}).get('fingerprint') != self.fingerprint):
                raise LicenseRejected('Machine activation could not be validated.')
            machine_id = machine['id']
            validated, meta = self._validate(key, {**scopes, 'machine': machine_id})
            if not meta['valid']:
                self._reject_validation(meta, key)
            self._check_license(validated)
            if validated['id'] != resource['id']:
                raise LicenseRejected('License identity mismatch.')
        else:
            self._check_license(resource)
        query = parse.urlencode({'ttl': config.OFFLINE_SECONDS, 'include': 'license', 'algorithm': 'base64+ed25519'})
        response = self._api('POST', machine_path + '/actions/check-out?' + query, key)
        try:
            certificate = response['data']['attributes']['certificate']
        except (KeyError, TypeError) as exc:
            raise NetworkError('Keygen did not return a machine certificate.') from exc
        verified = verify_machine_file(certificate, self.fingerprint, self._now())
        if verified['license_id'] != resource['id'] or (machine_id and verified['machine_id'] != machine_id):
            raise LicenseRejected('Machine certificate identity mismatch.')
        with self.lock:
            if generation != self.generation:
                raise LicenseError('The license session changed. Try again.')
            if allow_registration:
                self._forget_key()
            self._atomic_write(self.path, certificate)
            if allow_registration:
                self.generation += 1
            self.logged_out = False
            self.next_refresh = time.monotonic() + REFRESH_INTERVAL
            if not remember or not allow_registration:
                return remember
            try:
                keyring.set_password(SERVICE, self.fingerprint, key)
                return True
            except Exception:
                return False

    def _saved_key(self):
        try:
            return keyring.get_password(SERVICE, self.fingerprint)
        except Exception as exc:
            raise LicenseError('Windows credential storage is unavailable.') from exc

    def _forget_key(self):
        try:
            if self._saved_key():
                keyring.delete_password(SERVICE, self.fingerprint)
        except Exception as exc:
            raise LicenseError('Could not clear the saved license key from Windows Credential Manager.') from exc

    def logout(self):
        with self.lock:
            self.generation += 1
            self.logged_out = True
            self.path.unlink(missing_ok=True)
            self._forget_key()

    def refresh(self):
        with self.lock:
            generation = self.generation
        return self._refresh(generation)

    def _refresh(self, generation):
        with self.lock:
            if self.logged_out or generation != self.generation:
                raise LicenseError('Activate your license to continue.')
            key = self._saved_key()
        if not key:
            raise LicenseError('Enter your license key to refresh this computer.')
        try:
            return self._activate(key, True, generation, allow_registration=False)
        except (ApiError, LicenseRejected) as exc:
            if isinstance(exc, LicenseRejected) or exc.status in (401, 404) or exc.code in INVALID_LICENSE_CODES:
                with self.lock:
                    if generation == self.generation:
                        self.generation += 1
                        self.logged_out = True
                        self.path.unlink(missing_ok=True)
            raise

    def refresh_in_background(self):
        with self.lock:
            if self.logged_out or self.refreshing or time.monotonic() < self.next_refresh:
                return
            generation = self.generation
            self.refreshing = True

        def work():
            delay = REFRESH_INTERVAL
            try:
                self._refresh(generation)
            except (NetworkError, OSError):
                delay = NETWORK_RETRY_INTERVAL
            except ApiError as exc:
                if exc.status == 429 or exc.status >= 500:
                    delay = NETWORK_RETRY_INTERVAL
            except LicenseError:
                pass
            finally:
                with self.lock:
                    if generation == self.generation:
                        self.next_refresh = time.monotonic() + delay
                    self.refreshing = False

        threading.Thread(target=work, daemon=True).start()

    def deactivate(self, key=None):
        key = key or self._saved_key()
        if not key: raise LicenseError('Refresh using your license key before deactivating.')
        self._api('DELETE', '/machines/' + self.fingerprint, key); self.logout()
