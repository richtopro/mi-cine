# Mi Cine — servidor para Ultra.cc

Con el servidor, Mi Cine lee tus pelis y series de Ultra.cc. Tiene **usuarios con contraseña** y un **panel para dar de alta y de baja a tus familiares**. No depende de las cuentas de Google.

- Web: `https://TU-USUARIO.TU-SERVIDOR.usbx.me/micine/`
- Cada usuario tiene sus propios perfiles, su progreso y su lista.
- Las carátulas y las tendencias se buscan una sola vez en el servidor, y valen para todos.

## Instalación (una vez, unos 5 minutos)

1. Sube a tu repositorio de GitHub `mi-cine` la carpeta **`server`** (arrástrala entera a *Add file → Upload files*) y el **`index.html`** nuevo.
2. Conéctate a tu Ultra.cc por **SSH**. Los datos están en el panel de Ultra.cc, en la pestaña *Connect*.
   - En Windows: abre *PowerShell* y escribe `ssh TU-USUARIO@TU-SERVIDOR.usbx.me`.
3. Pega este comando y pulsa Enter:

   ```bash
   bash <(curl -fsSL https://raw.githubusercontent.com/richtopro/mi-cine/main/server/install.sh)
   ```

4. Te preguntará:
   - **Puerto**: elige uno de la lista de puertos libres que te muestra.
   - **Carpeta** con tus pelis y series, por ejemplo `~/media`.
   - **Clave de TMDB**: pégala; no se verá mientras escribes.
   - **Tu usuario administrador** y su contraseña.
5. Al terminar te dará la dirección. Ábrela, entra con tu usuario y espera al primer escaneo. Verás el progreso en *avatar → Panel de administración*.

## Uso diario

- **Añadir o quitar familiares**: *avatar → Panel de administración → Usuarios*. Allí puedes crear usuarios, cambiar contraseñas, desactivarlos o borrarlos.
- **Novedades**: el servidor revisa las carpetas cada 30 minutos. También puedes pulsar *Escanear ahora*.
- **Formatos**: el navegador reproduce MP4 y la mayoría de MKV con H.264. Si un vídeo no se puede reproducir, te ofrece abrirlo en VLC u otra app. La app de Android TV lo reproducirá todo.

## Comandos útiles (por SSH)

```bash
systemctl --user status micine      # ¿está funcionando?
systemctl --user restart micine     # reiniciarlo
tail -n 50 ~/.micine/micine.log     # ver el registro
cd ~/micine/server && MICINE_CONFIG=config.json python3 micine.py passwd NOMBRE   # cambiar una contraseña
bash <(curl -fsSL https://raw.githubusercontent.com/richtopro/mi-cine/main/server/install.sh)   # actualizar a la última versión
```

## Añadir más carpetas (Drive, NAS…)

Edita `~/micine/server/config.json` y añade entradas a `media`, por ejemplo:

```json
"media": [
  {"name": "Ultra.cc", "path": "~/media"},
  {"name": "Drive",    "path": "~/mnt/gdrive/Pelis"}
]
```

Después ejecuta `systemctl --user restart micine`. Drive y el NAS se montan con rclone; los configuraremos en el siguiente paso.
