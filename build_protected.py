"""Build a protected Windows release without shipping Python source."""
import ast, os, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON = ROOT / 'Salikha Photoman Ico.ico'
MODULES = ['license_config', 'licensing', 'licensing_ui', 'salikha_pro']

def main():
    if sys.platform != 'win32': raise SystemExit('Build this protected release on Windows.')
    stage = ROOT / 'protected-build' / 'win32'; source = stage / 'compile-source'; source.mkdir(parents=True, exist_ok=True)
    for name in MODULES: shutil.copy2(ROOT / (name + '.py'), source / (name + '.py'))
    setup = "from setuptools import setup\nfrom Cython.Build import cythonize\nsetup(ext_modules=cythonize(%r, build_dir='generated', compiler_directives={'language_level':3, 'binding':False, 'embedsignature':False}, annotate=False))\n" % [name + '.py' for name in MODULES]
    (source / 'setup.py').write_text(setup)
    subprocess.run([sys.executable, 'setup.py', 'build_ext', '--build-lib', str(stage / 'modules'), '--build-temp', str(stage / 'objects')], cwd=source, check=True)
    bundle = stage / 'modules'; (bundle / 'launcher.py').write_text('from salikha_pro import main\nmain()\n')
    hidden = set(MODULES)
    for name in MODULES:
        for node in ast.walk(ast.parse((ROOT / (name + '.py')).read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import): hidden.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module: hidden.add(node.module)
    hidden.update(['PIL.ImageWin', 'win32print', 'win32ui', 'keyring.backends.Windows', 'tkinter.scrolledtext', 'tkinter.messagebox', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.simpledialog'])
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--name', 'Salikha Photoman', '--distpath', str(ROOT / 'protected-dist' / 'win32'), '--workpath', str(stage / 'pyinstaller'), '--specpath', str(stage), '--paths', str(bundle), '--collect-all', 'keyring', '--collect-all', 'certifi', '--noupx']
    args += ['--icon', str(ICON), '--add-data', f'{ICON}:.']
    for name in sorted(hidden): args += ['--hidden-import', name]
    env = os.environ.copy(); env.pop('PYTHONPATH', None)
    subprocess.run(args + [str(bundle / 'launcher.py')], cwd=bundle, env=env, check=True)
    print('Protected release created in', ROOT / 'protected-dist' / 'win32')

if __name__ == '__main__': main()
