# TrackFlow · Hardening del servidor Linux

27 de noviembre de 2026 · OWASP Top 10:2021 · Validación en laboratorio focal.

## Alcance y resultado

**PASS técnico, incluida la persistencia tras arranque en frío.** La VM autorizada ejecutó Ubuntu Server 24.04.5 LTS con 1 vCPU y 2048 MB. Se usaron las imágenes preconstruidas y verificadas de la API y el Backoffice originales y PostgreSQL aislado. La cuenta y los datos de prueba fueron sintéticos. La red de contenedores fue interna y el anfitrión solo publicó SSH y HTTPS en loopback.

| Control | Antes | Después y tras el arranque en frío |
| --- | --- | --- |
| Administración SSH | Escucha privada con llave válida. La política permitía contraseña y acceso directo de root por llave. | `PermitRootLogin no`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no` y `AuthenticationMethods publickey`. Se verificó una nueva sesión autorizada, el rechazo de root y que solo se ofrecía llave. |
| Identidad operativa | La cuenta administrativa elevaba privilegios desde consola. | Cuenta operativa dedicada sin sudo ni acceso al socket Docker. API y Backoffice ejecutados con UID distintos de cero. |
| Permisos | Modos de SSH y rutas de aplicación inventariados. | SSH y directorios sensibles 0700, archivos sensibles 0600 y logs 0640. Código verificado sin escritura para las identidades de aplicación. Persistencia sintética escribible por su UID autorizado. |
| Firewall | Listener sintético temporal accesible desde el origen de prueba. | UFW activo y persistente. Entrada y reenvío denegados por defecto. Solo SSH y HTTPS desde el origen administrativo previsto. El listener respondió dentro del invitado y fue bloqueado desde fuera después del cambio y tras el arranque en frío. IPv6 mantuvo política de entrada restrictiva. |
| HTTPS y operación | Cadena de laboratorio válida. API, Backoffice, login y perfil sintético operativos. | Se repitieron las mismas comprobaciones con CA explícita y nombre verificado. Salud, login y perfil devolvieron 200. Perfil sin autenticar devolvió 401. El perfil existente se recuperó sin registrarlo de nuevo. |
| Recuperación administrativa | Consola disponible, configuración anterior respaldada y sesión SSH verificada. | La sesión ya abierta sobrevivió y una sesión nueva funcionó. Se comprobó la disponibilidad de la consola y se conservó un procedimiento de reversión con copias protegidas. La restauración completa se mantiene fuera de la prueba. |

## Evidencia reproducible

1. Registrar `sshd -t` y `sshd -T` antes y después. Mantener la consola disponible y comprobar una nueva sesión por llave antes de cerrar la sesión de recuperación.
2. Confirmar `ufw status verbose`, reglas IPv6, listeners con `ss -ltn` y bindings con `docker inspect`. Probar SSH y HTTPS desde el origen autorizado y un listener temporal que responda dentro del invitado y quede bloqueado desde afuera. Retirar el listener y su reenvío al finalizar.
3. Verificar identidades y permisos con `id`, `sudo -l -U` y `stat`. Comprobar que el operador carece de acceso a secretos y al socket Docker y que el proceso API puede escribir en su destino sintético autorizado.
4. Ejecutar `curl --cacert` con la CA del laboratorio y verificar el nombre del certificado. Comprobar salud de API, página de login, autenticación y lectura del mismo perfil sintético antes y después. Comprobar que el PostgreSQL aislado conserva su volumen y esquema.
5. Confirmar apagado ACPI, estado apagado, nuevo identificador de arranque y arranque automático de Docker Engine, Nginx, API, Backoffice y PostgreSQL. Repetir las comprobaciones anteriores tras la disponibilidad efectiva de la API.

SSH respondió aproximadamente en 55 segundos desde la orden de arranque y systemd terminó en 44 segundos. El 502 transitorio durante la inicialización de la API se resolvió antes del smoke. Se observaron 1350 MiB de RAM disponible, swap sin uso y 19 GiB libres. Estas muestras corresponden al perfil focal.

## Límites

El registro privilegiado del arranque verificado quedó sin nuevos avisos de soft lockup, RCU u OOM. El arranque anterior registró avisos de watchdog. La causa y la estabilidad prolongada quedan por acreditar.

El alcance del laboratorio cubre la API, el Backoffice, PostgreSQL sintético, autenticación local y proxy HTTPS. La recuperación de componentes y la reconstrucción aislada de identidad se documentaron por separado. La recuperación integral del entorno de aceptación y de su Backoffice conectado a todas las integraciones queda por verificar. La CA, los respaldos y los registros operativos se custodian fuera de este documento.

[Auditoría OWASP](OWASP_TOP10_AUDIT.md) · [Remediación](SECURITY_REMEDIATION.md) · [Overlay de despliegue](../../infra/docker-compose.deploy.yml) · [OpenSSH en Ubuntu](https://ubuntu.com/server/docs/how-to/security/openssh-server/) · [Firewall en Ubuntu](https://ubuntu.com/server/docs/how-to/security/firewalls/)
