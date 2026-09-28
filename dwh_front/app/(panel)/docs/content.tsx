import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import {
  BookOpen,
  Rocket,
  Users,
  Building2,
  Boxes,
  MonitorSmartphone,
  HeartPulse,
  Siren,
  BellRing,
  GitCompareArrows,
  UserCog,
  HelpCircle,
} from "lucide-react";
import { FlowDiagram } from "@/components/docs/flow-diagram";

export interface DocSection {
  id: string;
  title: string;
  icon: LucideIcon;
  /** Texto adicional (fuera de los nodos de React) que también se busca al filtrar. */
  keywords?: string;
  body: ReactNode;
}

/* ── Presentación reutilizable, consistente con components/ui/primitives.tsx ── */

function P({ children }: { children: ReactNode }) {
  return <p className="text-sm leading-6 text-slate-600">{children}</p>;
}

function H3({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <h3 id={id} className="mt-6 text-base font-semibold text-slate-900 scroll-mt-20">
      {children}
    </h3>
  );
}

function UL({ children }: { children: ReactNode }) {
  return <ul className="list-disc space-y-1.5 pl-5 text-sm leading-6 text-slate-600">{children}</ul>;
}

function OL({ children }: { children: ReactNode }) {
  return <ol className="list-decimal space-y-1.5 pl-5 text-sm leading-6 text-slate-600">{children}</ol>;
}

/** Nombre exacto de un botón, campo, pestaña o menú del panel (p. ej. «Probar conexión»). */
function UiLabel({ children }: { children: ReactNode }) {
  return <span className="rounded border border-slate-200 bg-slate-100 px-1.5 py-0.5 font-mono text-[12.5px] font-medium text-slate-700">{children}</span>;
}

/** Ruta o comando corto. */
function Code({ children }: { children: ReactNode }) {
  return <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[12.5px] text-slate-700">{children}</code>;
}

const toneClasses = {
  info: "border-brand-200 bg-brand-50 text-brand-900",
  warn: "border-amber-200 bg-amber-50 text-amber-900",
  perm: "border-violet-200 bg-violet-50 text-violet-900",
} as const;

/** Aviso destacado. tone="perm" para "qué permiso necesita esta acción". */
function Callout({ tone = "info", title, children }: { tone?: keyof typeof toneClasses; title?: string; children: ReactNode }) {
  return (
    <div className={`rounded-md border px-3.5 py-2.5 text-sm leading-6 ${toneClasses[tone]}`}>
      {title && <p className="mb-0.5 font-semibold">{title}</p>}
      <div className="text-[13px] opacity-95">{children}</div>
    </div>
  );
}

function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-md border border-slate-200">
      <table className="w-full min-w-[420px] text-left text-sm">{children}</table>
    </div>
  );
}
function THead({ cols }: { cols: string[] }) {
  return (
    <thead className="bg-slate-50 text-xs font-semibold uppercase tracking-wide text-slate-500">
      <tr>
        {cols.map((c) => (
          <th key={c} className="px-3 py-2">
            {c}
          </th>
        ))}
      </tr>
    </thead>
  );
}
function Row({ cells }: { cells: ReactNode[] }) {
  return (
    <tr className="border-t border-slate-100">
      {cells.map((c, i) => (
        <td key={i} className="px-3 py-2 align-top text-slate-600">
          {c}
        </td>
      ))}
    </tr>
  );
}

/* ─────────────────────────────────────────────────────────────────────────── */

export const SECTIONS: DocSection[] = [
  {
    id: "introduccion",
    title: "Introducción",
    icon: BookOpen,
    keywords: "qué es nexus dwh resumen flujo general arquitectura",
    body: (
      <div className="space-y-4">
        <P>
          <b>Nexus DWH</b> centraliza, en una sola base de configuración, las conexiones de origen (el sistema del
          cliente: SQL Server, MySQL, Firebird…) y de destino (el almacén de datos, DWH) de todas las empresas y
          agencias que administra. Un agente instalado en la sede del cliente (<b>NexusAgent</b>) extrae los datos
          de origen y los carga en el DWH; el panel web es donde se da de alta y se supervisa todo eso: grupos,
          empresas, agencias, catálogo de tablas, extractores, salud, incidencias y auditoría.
        </P>
        <P>
          <b>Regla de arquitectura, siempre vigente:</b> el agente es quien <b>inicia</b> todas las conexiones —hacia
          el origen, hacia el DWH y hacia Nexus por HTTPS—. Nexus <b>nunca</b> se conecta a la red del cliente ni a
          sus bases de datos; ni siquiera para «Probar conexión» (sección «Grupos y destino DWH»), que en realidad la
          ejecuta el agente y solo reporta el resultado.
        </P>
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <FlowDiagram className="mx-auto max-w-[720px]" />
        </div>
        <P>
          En resumen: el agente extrae del <b>DMS de origen</b> y carga (upsert) en el <b>DWH destino</b>; para
          hacerlo, primero pide su configuración y tareas al <b>backend</b> de Nexus (FastAPI) a través de una
          conexión HTTPS saliente. El backend guarda esa configuración y los eventos que reporta el agente en su{" "}
          <b>base de datos de configuración</b>, y es también lo que consulta el <b>panel web</b> (a través de un
          proxy interno <UiLabel>/admin</UiLabel>) para que usted vea y administre todo desde el navegador.
        </P>
      </div>
    ),
  },
  {
    id: "primeros-pasos",
    title: "Primeros pasos",
    icon: Rocket,
    keywords: "iniciar sesión login cambiar contraseña roles alcance grupo correo contraseña",
    body: (
      <div className="space-y-4">
        <H3 id="ps-login">Iniciar sesión</H3>
        <P>
          Abra el panel y entre a <Code>/login</Code> con su <b>correo y contraseña</b>. El acceso es por
          correo (no por nombre de usuario). No hay correo ni contraseña por defecto: un administrador debe
          crear su cuenta primero, con su correo, en «Usuarios y Auditoría». Si su cuenta no tiene correo
          asignado, no podrá iniciar sesión hasta que un administrador se lo asigne.
        </P>
        <P>
          La sesión tiene una duración máxima desde el inicio de sesión y se cierra sola tras un tiempo sin
          actividad (por defecto 12 horas y 30 minutos; el administrador del sistema puede ajustarlos). Si se equivoca varias veces con la contraseña, su cuenta se bloquea temporalmente (el tiempo de
          bloqueo aumenta con cada intento fallido); espere e intente de nuevo, o pida a un administrador que la
          desbloquee (<UiLabel>Desbloquear</UiLabel>, en Usuarios).
        </P>
        <H3 id="ps-cambiar">Cambiar contraseña</H3>
        <P>
          Si un administrador le reinició la contraseña o le creó la cuenta, el panel lo llevará automáticamente a{" "}
          <Code>/cambiar-contrasena</Code> y no le dejará usar el resto del panel hasta que la cambie. También puede
          cambiarla usted mismo en cualquier momento desde esa misma pantalla. La contraseña nueva debe tener al
          menos la longitud mínima que exija el sistema (el formulario la indica), no puede contener su nombre de usuario, no puede ser una contraseña obvia/común y debe
          ser distinta de la actual. Al cambiarla se cierran las demás sesiones abiertas de su cuenta.
        </P>
        <H3 id="ps-roles">Roles y alcance por grupo</H3>
        <P>
          Cada usuario tiene uno o varios <b>roles</b> (por ejemplo, <UiLabel>operador</UiLabel> o{" "}
          <UiLabel>admin_config</UiLabel>), y cada asignación de rol tiene un <b>alcance</b>: o bien{" "}
          <b>todos los grupos</b>, o bien <b>un grupo concreto</b>. Así, puede ser operador en el grupo «Grupo A» y
          solo lectura en el grupo «Grupo B», por ejemplo. Lo que ve y puede hacer en cada pantalla depende de sus
          permisos sobre el grupo del recurso: un grupo, empresa, agencia o extractor que esté fuera de su alcance
          simplemente no aparece en las listas, y si intenta ir a su URL directamente el panel responde «no
          encontrado» (el backend no distingue entre «no existe» y «no es suyo», por seguridad).
        </P>
        <P>
          Si un botón o campo no debería estar disponible para usted, el panel lo oculta o lo deshabilita; si aun así
          intenta una acción sin permiso, el backend la rechaza y el panel muestra «Sin permiso». El menú izquierdo
          muestra su nombre, usuario y un resumen de su alcance en la parte inferior.
        </P>
      </div>
    ),
  },
  {
    id: "grupos-destino",
    title: "Grupos y destino DWH",
    icon: Users,
    keywords:
      "grupo destino dwh esquema ssl tls probar conexión certificado ca destino por empresa reinicio de carga sslmode",
    body: (
      <div className="space-y-4">
        <H3 id="gd-que-es">Qué es un grupo</H3>
        <P>
          Un <b>grupo</b> (menú <UiLabel>Grupos</UiLabel>) agrupa una o varias empresas que comparten, por defecto, el
          mismo <b>destino DWH</b> (el almacén de datos donde se carga la información). Al crear o editar un grupo se
          define su nombre, si está habilitado y —solo si tiene el permiso <UiLabel>Administrar credenciales</UiLabel>
          — el destino: host, puerto, base, usuario y contraseña. Sin ese permiso, el panel muestra «Destino DWH
          oculto» en vez de esos datos (no son visibles, aunque usted pueda ver el resto del grupo).
        </P>
        <H3 id="gd-esquema-ssl">Esquema destino y SSL/TLS</H3>
        <P>
          En la edición de un grupo, la sección <b>«Destino: Data Warehouse»</b> tiene, además de host/base/usuario:
        </P>
        <UL>
          <li>
            <b>Esquema destino</b>: el esquema de PostgreSQL donde se cargan las tablas del catálogo que no traen un
            esquema explícito en su nombre (por defecto <Code>public</Code>). Una tabla del catálogo escrita como{" "}
            <Code>dwh.carter</Code> siempre respeta ese esquema, sin importar lo que diga aquí.
          </li>
          <li>
            <b>SSL/TLS</b> (<Code>sslmode</Code>): el nivel de cifrado exigido para conectarse al DWH. De menos a más
            estricto: <Code>disable</Code>, <Code>allow</Code>, <Code>prefer</Code> (por defecto: cifra si el
            servidor lo ofrece, sin verificar el certificado), <Code>require</Code> (exige TLS), <Code>verify-ca</Code>{" "}
            (exige además el certificado de la CA) y <Code>verify-full</Code> (recomendado si el tráfico sale de la
            red local: verifica la CA y el nombre del servidor). El panel pide el <b>certificado de la CA (PEM)</b>{" "}
            cuando el modo lo requiere.
          </li>
        </UL>
        <Callout tone="perm" title="Permiso necesario">
          Ver y cambiar host/base/usuario/contraseña, esquema, SSL y CA del grupo: <UiLabel>Administrar credenciales</UiLabel>{" "}
          sobre ese grupo. Nombre y habilitado: <UiLabel>Administrar configuración</UiLabel>.
        </Callout>
        <H3 id="gd-destino-empresa">Destino por empresa</H3>
        <P>
          Cada empresa puede, en vez de usar el destino de su grupo, tener su <b>propio destino completo</b>{" "}
          (host, base, usuario, contraseña, esquema y SSL propios). Esto se configura en <UiLabel>Empresas</UiLabel> →
          editar, con el interruptor <UiLabel>Usar el destino del grupo</UiLabel> (activado por defecto). Al
          desactivarlo aparecen los campos de un destino propio; no se pueden mezclar campos del grupo con campos
          propios: es todo de uno o todo del otro. Si vuelve a activarlo, el destino propio guardado se borra (no
          quedan credenciales sin usar).
        </P>
        <H3 id="gd-probar-conexion">Probar conexión y qué significa cada resultado</H3>
        <P>
          El botón <UiLabel>Probar conexión</UiLabel> (en el grupo, y también en la empresa como{" "}
          <UiLabel>Probar conexión al destino (del grupo)</UiLabel> / <UiLabel>Probar conexión al destino propio</UiLabel>
          , y <UiLabel>Probar conexión al origen</UiLabel> para el DMS) no lo ejecuta Nexus: pide a un{" "}
          <b>agente en línea</b> con alcance sobre esa conexión que la intente, usando la configuración{" "}
          <b>ya guardada</b> (si tiene cambios sin guardar, el botón se deshabilita hasta que los guarde). El
          resultado avanza por estos estados:
        </P>
        <Table>
          <THead cols={["Estado", "Significado"]} />
          <tbody>
            <Row cells={[<UiLabel key="1">Esperando a un agente…</UiLabel>, "La prueba se encoló; ningún agente la ha tomado todavía."]} />
            <Row cells={[<UiLabel key="1">El agente está probando…</UiLabel>, "Un agente la tomó y está conectándose."]} />
            <Row cells={[<UiLabel key="1">Conexión correcta</UiLabel>, "El agente pudo conectarse y usar (o crear) el esquema destino, o consultar el origen."]} />
            <Row
              cells={[
                <UiLabel key="1">Falló</UiLabel>,
                <>
                  El agente no pudo conectarse o le faltan privilegios; muestra un código estable (p. ej.{" "}
                  <Code>DWH_AUTH_FAILED</Code>, <Code>DWH_SSL_ERROR</Code>, <Code>DWH_INSUFFICIENT_PRIVILEGE</Code>) y
                  un mensaje sin datos sensibles.
                </>,
              ]}
            />
            <Row cells={[<UiLabel key="1">Sin respuesta</UiLabel>, "Un agente la tomó pero no llegó a reportar el resultado en el plazo esperado (2 min); intente de nuevo."]} />
            <Row
              cells={[
                <UiLabel key="1">Sin agente en línea</UiLabel>,
                "No hay ningún agente conectado (versión 5.3 o superior) con alcance sobre esa conexión en este momento. Revise Instalaciones/Salud: el agente puede estar apagado, desconectado o ser demasiado viejo.",
              ]}
            />
          </tbody>
        </Table>
        <Callout tone="perm" title="Permiso necesario">
          Iniciar la prueba: <UiLabel>Administrar configuración</UiLabel> sobre el grupo. Ver el resultado:{" "}
          <UiLabel>Consultar</UiLabel>.
        </Callout>
        <H3 id="gd-reinicio">Reinicio de carga al cambiar destino</H3>
        <P>
          Si el cambio de destino mueve la <b>ubicación física</b> de los datos (cambia host, puerto, base o
          esquema —no si solo cambia el usuario, la contraseña o el SSL—), Nexus reinicia automáticamente el punto de
          sincronización de los extractores de las empresas afectadas: la próxima ejecución hará una{" "}
          <b>carga completa</b> en el destino nuevo, igual que con <UiLabel>Reiniciar última ejecución</UiLabel>. El
          panel avisa antes de guardar («Cambia el destino: al guardar se reiniciará la carga de N extractor(es)») con
          el interruptor <UiLabel>Reiniciar la carga (recomendado)</UiLabel>, activado por defecto; desactívelo solo si
          sabe que sus extractores no tienen claves de upsert (una recarga completa sin claves puede duplicar filas).
        </P>
      </div>
    ),
  },
  {
    id: "empresas-agencias",
    title: "Empresas y Agencias",
    icon: Building2,
    keywords: "empresa razón social agencia sede origen dms",
    body: (
      <div className="space-y-4">
        <H3 id="ea-empresas">Empresas (origen DMS)</H3>
        <P>
          Una <b>empresa</b> (razón social) es la unidad que tiene su propio <b>origen</b> (el DMS del cliente:
          host/base/usuario/contraseña de SQL Server, MySQL, etc.) y, si así se configura, su propio destino DWH
          (sección anterior). Se administra en <UiLabel>Empresas</UiLabel>: alta, edición, habilitar/deshabilitar,
          tokens (mostrar/copiar/regenerar) y, con permiso, <UiLabel>Probar conexión al origen</UiLabel>. Cada empresa
          pertenece a un grupo.
        </P>
        <H3 id="ea-agencias">Agencias</H3>
        <P>
          Una <b>agencia</b> es una sede física de una empresa. El servidor de <b>origen</b> no se configura por
          agencia: sale siempre de la empresa a la que pertenece. Lo que sí cambia por agencia son sus{" "}
          <b>extractores</b> (siguiente sección). Se administra en <UiLabel>Agencias</UiLabel>, con las mismas
          acciones que empresas (alta, edición, habilitar/deshabilitar, tokens).
        </P>
        <H3 id="ea-detalle">Detalle de grupo y de agencia</H3>
        <P>
          <Code>/grupos/[id]</Code> muestra, por empresa, sus agencias, y por cada agencia sus extractores con salud,
          programación y última carga exitosa; tiene tarjetas de resumen (agencias, extractores activos, con error,
          retrasados, sin ejecutar) que al pulsarlas filtran la tabla, botones <UiLabel>Expandir todo</UiLabel> /{" "}
          <UiLabel>Contraer todo</UiLabel> y <UiLabel>Nuevo extractor</UiLabel> (elige la agencia entre las del
          grupo). <Code>/agencias/[id]</Code> muestra el detalle completo de una agencia: sus extractores, incidencias
          abiertas y últimas 25 ejecuciones, con <UiLabel>Nuevo extractor</UiLabel> (agencia ya preseleccionada) y{" "}
          <UiLabel>Clonar a otras agencias</UiLabel> para varios extractores a la vez.
        </P>
      </div>
    ),
  },
  {
    id: "catalogo-extractores",
    title: "Catálogo de objetos y Extractores",
    icon: Boxes,
    keywords:
      "catalogo objeto tabla destino create_table_sql upsert_keys extractor tarea crear editar activar desactivar clonar reiniciar última ejecución",
    body: (
      <div className="space-y-4">
        <H3 id="ce-catalogo">Catálogo de objetos</H3>
        <P>
          El <b>catálogo</b> (menú <UiLabel>Catálogo de objetos</UiLabel>) define, por empresa, qué tablas se pueden
          cargar en el DWH: tabla destino, SQL de creación (<Code>create_table_sql</Code>), claves de upsert (
          <Code>upsert_keys</Code>), restricción y columnas estáticas (<Code>static_columns</Code>), en editores de
          texto monoespaciados. Un extractor siempre apunta a un objeto del catálogo.
        </P>
        <H3 id="ce-extractores">Extractores</H3>
        <P>
          En el panel, las <b>tareas</b> se llaman <b>extractores</b>: un extractor es la combinación de una{" "}
          <b>agencia</b> + un <b>objeto del catálogo</b> (de la empresa de esa agencia) + el SQL de extracción, la
          programación y los umbrales de salud. Se administran en <UiLabel>Tareas</UiLabel>, o directamente desde el
          detalle de un grupo o de una agencia.
        </P>
        <OL>
          <li>
            <b>Crear</b>: botón <UiLabel>Nuevo extractor</UiLabel> (en Tareas, en el detalle de grupo o en el de
            agencia). Se elige agencia y objeto del catálogo, se escribe el SQL de extracción (<Code>extract_sql</Code>
            ), la programación (<Code>schedule_seconds</Code>, con atajos como «cada 15 min») y, opcionalmente,
            duración esperada y tolerancia de retraso.
          </li>
          <li>
            <b>Editar</b>: abre el mismo formulario; sin el permiso <UiLabel>Administrar configuración</UiLabel> se
            abre en solo lectura (se puede ver el SQL sin poder guardar).
          </li>
          <li>
            <b>Activar/desactivar</b>: interruptor <b>Activo</b> en la fila del extractor. Al desactivarlo, deja de
            programarse y sus incidencias abiertas se cierran automáticamente con el motivo «tarea deshabilitada».
          </li>
          <li>
            <b>Clonar a otras agencias</b>: botón <UiLabel>Clonar</UiLabel> en un extractor, o{" "}
            <UiLabel>Clonar a otras agencias</UiLabel> para clonar varios de una agencia a la vez. El diálogo pide las
            agencias destino (agrupadas por grupo/empresa, con búsqueda y la marca «Ya lo tiene»), opciones (si el
            clon queda activo o no —por defecto queda <b>deshabilitado</b> para revisarlo—, qué hacer si ya existe:
            omitir o actualizar) y siempre exige una <b>vista previa</b> (<UiLabel>Vista previa</UiLabel>) antes de
            confirmar: simula el resultado sin guardar nada. Si el objeto del catálogo no existe todavía en la
            empresa destino se copia; si existe con otra definición, se marca como <b>conflicto</b> y no se toca
            salvo que decida sobrescribirlo explícitamente.
          </li>
          <li>
            <b>Reiniciar última ejecución</b>: botón <UiLabel>Reiniciar última ejecución</UiLabel>. Pone en blanco el
            punto de sincronización del extractor: la siguiente corrida hace una carga completa. Úselo si cambió de
            reloj de referencia, si sospecha que faltan datos o tras mover el extractor a otro destino manualmente.
          </li>
        </OL>
        <Callout tone="perm" title="Permiso necesario">
          Crear, editar, activar/desactivar, clonar y reiniciar: <UiLabel>Administrar configuración</UiLabel> sobre el
          grupo de origen y, para clonar, también sobre el grupo de cada agencia destino.
        </Callout>
      </div>
    ),
  },
  {
    id: "instalaciones-agente",
    title: "Instalaciones y agente",
    icon: MonitorSmartphone,
    keywords:
      "instalación agente enrolar token de un solo uso install_service update_agent rotar revocar credencial tareas retenidas actualizar versión desactualizada",
    body: (
      <div className="space-y-4">
        <H3 id="ia-enrolar">Enrolar con token (instalación nueva)</H3>
        <P>
          En el panel, copie el <b>token de enrolamiento</b> de la agencia o empresa (prefiera agencia/empresa sobre
          grupo: un token de grupo entrega credenciales de origen de <b>todas</b> las empresas del grupo). En la
          máquina de la sede, como administrador, ejecute:
        </P>
        <P>
          <Code>NexusAgent.exe --selftest</Code> y luego{" "}
          <Code>scripts\install_service.ps1 -PackageDir . -ApiUrl https://…​ -TokenType agency</Code>. El instalador
          crea el servicio de Windows «Nexus DWH Agent», pide el <b>token de un solo uso</b> (sin mostrarlo en
          pantalla) y lo consume automáticamente al primer arranque: tras enrolarse una vez, el token deja de ser
          válido y el agente opera con su propia credencial (nunca vuelve a usar el token).
        </P>
        <P>Tras enrolar, revise en el panel: Instalaciones (aparece la máquina) y Salud (el latido llega cada minuto).</P>
        <H3 id="ia-rotar-revocar">Rotar o revocar la credencial</H3>
        <P>
          En <UiLabel>Instalaciones</UiLabel>, cada agente tiene los botones:
        </P>
        <UL>
          <li>
            <UiLabel>Rotar credencial</UiLabel>: pide al agente que renueve su credencial en su siguiente contacto,
            sin necesidad de volver a enrolar; el panel nunca ve el secreto, solo marca que se pidió la rotación.
          </li>
          <li>
            <UiLabel>Revocar instalación</UiLabel>: corta el acceso de esa instalación de inmediato; el agente se
            detiene y no vuelve a enrolarse solo. Para reactivarlo hay que ejecutar el enrolamiento otra vez con un
            token vigente (regenere el token si sospecha que se filtró).
          </li>
        </UL>
        <Callout tone="perm" title="Permiso necesario">
          Rotar y revocar: <UiLabel>Administrar credenciales</UiLabel> sobre el grupo de la instalación.
        </Callout>
        <H3 id="ia-retenidas">Tareas retenidas y actualizar a 5.3</H3>
        <P>
          Los agentes anteriores a la versión 5.3 no entienden destinos por empresa, SSL obligatorio ni esquemas con
          DDL de catálogo. Cuando eso aplica, Nexus <b>retiene</b> esas tareas en vez de entregárselas a un agente
          viejo (para no cargar mal). En <UiLabel>Instalaciones</UiLabel> verá el aviso «Tareas retenidas: N —
          actualice el agente a 5.3»: es la señal de que debe actualizar esa instalación antes de usar esas
          funciones.
        </P>
        <H3 id="ia-actualizar">Actualizar el agente</H3>
        <P>
          Copie el paquete nuevo a la máquina y, como administrador, ejecute <b>siempre el script de la versión ya
          instalada</b> (no el que trae el paquete nuevo):
        </P>
        <P>
          <Code>
            {`"C:\\Program Files\\NexusAgent\\scripts\\update_agent.ps1" -PackageDir C:\\NexusAgentPkg\\NexusAgent-5.3.0`}
          </Code>
        </P>
        <P>
          El script detiene el servicio, respalda la versión anterior, instala la nueva y comprueba que arrancó
          correctamente; si algo falla, restaura la versión anterior automáticamente y deja el servicio en marcha
          (nunca queda el programa ausente ni el servicio detenido en silencio). Los datos (credencial, cola,
          agenda) no se tocan.
        </P>
        <H3 id="ia-desactualizada">Versión desactualizada</H3>
        <P>
          El panel marca una instalación como <b>Desactualizada</b> cuando reporta una versión menor a la última
          publicada; es solo informativo (no bloquea al agente), pero conviene actualizarlo para tener las funciones
          más recientes y las correcciones de seguridad.
        </P>
      </div>
    ),
  },
  {
    id: "salud-ejecuciones",
    title: "Salud y Ejecuciones",
    icon: HeartPulse,
    keywords: "salud estados al día ok en curso con error fallando retrasada deshabilitada sin ejecuciones sin ejecutar columnas conectividad ejecuciones",
    body: (
      <div className="space-y-4">
        <H3 id="se-salud">Salud</H3>
        <P>
          La pantalla <UiLabel>Salud</UiLabel> tiene dos tablas: <b>instalaciones</b> (conectividad, último contacto,
          última ejecución, última carga exitosa, cola/apartados/descartes, incidencias abiertas) y <b>tareas</b>{" "}
          (estado, última ejecución, última carga exitosa, punto de sincronización, error actual, errores
          consecutivos y plazos). Se actualiza sola cada 30 segundos. Recuerde: <b>conectividad no es lo mismo que
          éxito del ETL</b> — un agente puede estar en línea y, aun así, tener una tarea fallando.
        </P>
        <Table>
          <THead cols={["Estado de la tarea", "Significa"]} />
          <tbody>
            <Row cells={[<UiLabel key="1">Al día</UiLabel>, "Todo en orden: última carga dentro del plazo esperado."]} />
            <Row cells={[<UiLabel key="1">En curso</UiLabel>, "Hay una ejecución corriendo ahora mismo, confirmada por el latido del agente."]} />
            <Row cells={[<UiLabel key="1">Con error</UiLabel>, "La ejecución más reciente falló o se interrumpió, o hay una incidencia de falla abierta para esa tarea."]} />
            <Row cells={[<UiLabel key="1">Retrasada</UiLabel>, "Pasó el plazo esperado (programación + duración + tolerancia) sin una carga nueva y sin estar en curso."]} />
            <Row cells={[<UiLabel key="1">Deshabilitada</UiLabel>, "El extractor, la agencia, la empresa, el grupo o el objeto están deshabilitados; nunca genera alertas de retraso."]} />
            <Row cells={[<UiLabel key="1">Sin ejecuciones</UiLabel>, "Nunca se ha ejecutado y todavía está dentro del plazo de gracia."]} />
          </tbody>
        </Table>
        <P>
          Filtre por grupo, empresa, agencia, conectividad y estado de tarea; los filtros quedan guardados en la URL
          para compartirlos o recargar la página sin perderlos.
        </P>
        <H3 id="se-ejecuciones">Ejecuciones</H3>
        <P>
          <UiLabel>Ejecuciones</UiLabel> es el historial por intento: cada fila es una corrida de un extractor con
          filas leídas/cargadas/insertadas/actualizadas, duración, código de error (si hubo), mensaje ya saneado y
          avisos. Filtre por grupo/empresa/agencia, estado, etapa del fallo (configuración, extracción,
          transformación, carga o reporte), instalación, extractor y rango de fechas.
        </P>
      </div>
    ),
  },
  {
    id: "incidencias",
    title: "Incidencias",
    icon: Siren,
    keywords: "incidencia categoría reconocer resolver cierre manual cola",
    body: (
      <div className="space-y-4">
        <P>
          Una <b>incidencia</b> es una alerta que Nexus abre solo (sin que nadie la registre a mano) cuando detecta un
          problema: una instalación desconectada, una tarea que falló, una tarea retrasada, una ejecución que va más
          lenta de lo normal, etc. El menú muestra un contador de incidencias abiertas sin reconocer (en rojo si hay
          críticas o de error).
        </P>
        <H3 id="in-categorias">Categorías</H3>
        <Table>
          <THead cols={["Categoría", "Severidad", "Se resuelve con…"]} />
          <tbody>
            <Row cells={["Desconectado", "Crítica", "Un latido nuevo del agente, o Nexus vuelve a ver contacto reciente."]} />
            <Row cells={["Falla de tarea", "Error (interrumpida: advertencia)", "Una carga confirmada de esa misma tarea en esa misma instalación."]} />
            <Row cells={["Tarea retrasada", "Advertencia", "Una carga que la deja al día."]} />
            <Row cells={["Ejecución prolongada", "Advertencia", "El fin de esa ejecución."]} />
            <Row cells={["Cambio de reloj del watermark", "Advertencia", "Un checkpoint aceptado, o Reiniciar última ejecución."]} />
            <Row cells={["Cola: apartados / descartes", "Advertencia / Error", <b key="1">Solo cierre manual</b>]} />
          </tbody>
        </Table>
        <H3 id="in-reconocer">Reconocer vs. resolver</H3>
        <P>
          <b>Reconocer</b> (botón <UiLabel>Reconocer</UiLabel>, con comentario opcional) solo indica que alguien la
          revisó: la incidencia sigue <b>abierta</b> y el estado técnico (la tarea sigue fallando, la instalación
          sigue desconectada, etc.) se sigue mostrando igual. <b>Resolver</b> no es una acción manual para la mayoría
          de las categorías: se resuelve <b>sola</b>, cuando llega la evidencia técnica correspondiente (una carga
          exitosa, un latido, etc.).
        </P>
        <H3 id="in-cierre-manual">Cierre manual (solo cola)</H3>
        <P>
          Las incidencias de <b>cola local</b> (reportes apartados o descartados en el agente) no tienen forma
          automática de resolverse —lo que se apartó o se descartó no vuelve—, así que se cierran con el botón{" "}
          <UiLabel>Cerrar con motivo</UiLabel>, que exige escribir el motivo. Intentarlo en cualquier otra categoría
          es rechazado por el backend: esas se resuelven solas con evidencia.
        </P>
        <Callout tone="perm" title="Permiso necesario">
          Reconocer: <UiLabel>Reconocer incidencias/eventos</UiLabel>. Cerrar de cola: <UiLabel>Cerrar incidencias de cola</UiLabel>.
        </Callout>
      </div>
    ),
  },
  {
    id: "notificaciones",
    title: "Notificaciones",
    icon: BellRing,
    keywords: "canal webhook firma hmac prueba enviar prueba",
    body: (
      <div className="space-y-4">
        <P>
          Las incidencias siempre se ven en el panel (menú Incidencias y el Dashboard), pero además se puede
          configurar un <b>canal de notificación</b> externo en <UiLabel>Notificaciones</UiLabel> para avisar cuando
          una incidencia se abre, se resuelve o (opcionalmente) sigue abierta sin reconocer tras un tiempo.
        </P>
        <H3 id="no-canal">Canal webhook</H3>
        <P>
          Al crear un canal se define la URL (HTTPS obligatorio salvo que se permita HTTP explícitamente), un secreto
          de firma opcional, filtros (severidad mínima, grupo, categorías, si notifica aperturas y/o resoluciones) y
          si envía recordatorios periódicos. La URL y el secreto son de <b>solo escritura</b>: una vez guardados, ni
          la API ni el panel los vuelven a mostrar (solo si están configurados o no).
        </P>
        <H3 id="no-firma">Firma HMAC</H3>
        <P>
          Si el canal tiene secreto, cada envío incluye la cabecera <Code>X-Nexus-Signature: sha256=…</Code>, un HMAC-
          SHA256 del cuerpo con marca de tiempo. El receptor debe recalcular esa firma, compararla en tiempo
          constante y rechazar marcas de tiempo viejas (más de unos minutos) para verificar que el mensaje viene de
          Nexus y no ha sido alterado.
        </P>
        <H3 id="no-prueba">Probar</H3>
        <P>
          Botón <UiLabel>Enviar prueba</UiLabel>: manda un evento de prueba (sin ninguna incidencia real) al canal y
          muestra si se entregó correctamente, para verificar la URL y la firma antes de depender de él.
        </P>
        <Callout tone="perm" title="Permiso necesario">
          Alta, edición y prueba de canales: <UiLabel>Administrar credenciales</UiLabel> sobre el grupo del canal (o
          alcance global para un canal de «todos los grupos»).
        </Callout>
      </div>
    ),
  },
  {
    id: "estructura",
    title: "Estructura",
    icon: GitCompareArrows,
    keywords:
      "estructura línea base cambios pendientes dar por entendido modificó cliente modificó equipo nexus reclasificar no se pudo verificar la estructura",
    body: (
      <div className="space-y-4">
        <P>
          <UiLabel>Estructura</UiLabel> compara, para cada base monitoreada (el DWH, y opcionalmente los orígenes),
          las tablas/vistas/columnas/restricciones actuales contra una <b>línea base aprobada</b>, para avisar si algo
          cambió por fuera de lo que Nexus esperaba (alguien agregó una columna, borró una tabla, etc.). No es una
          auditoría de filas: solo mira la estructura.
        </P>
        <H3 id="es-linea-base">Aprobar línea base</H3>
        <P>
          El primer inventario de una base monitoreada queda como <b>propuesta</b>: nunca se aprueba solo. Un
          administrador con permiso revisa la propuesta y la <b>aprueba</b> (puede aprobar solo una parte; lo no
          aprobado queda como «cambio pendiente»). A partir de ahí, cualquier diferencia frente a esa línea base
          genera una alerta.
        </P>
        <H3 id="es-pendientes">Cambios pendientes</H3>
        <P>
          Pestaña <b>Cambios pendientes</b>: una fila por objeto que cambió (se agregó, se modificó o se eliminó),
          con el detalle exacto (columna agregada, tipo cambiado, restricción distinta, etc.), cuándo se detectó por
          primera vez y cuándo se observó por última vez.
        </P>
        <H3 id="es-dar-por-entendido">«Dar por entendido»</H3>
        <P>
          Para cada cambio pendiente, el formulario <UiLabel>Dar por entendido</UiLabel> exige elegir un{" "}
          <b>responsable</b>: <UiLabel>Modificó cliente</UiLabel> o <UiLabel>Modificó equipo Nexus</UiLabel> (comentario
          y número de ticket opcionales). Esto incorpora <b>solo esa diferencia</b> a la línea base (no aprueba en
          bloque todo lo demás) y saca el cambio de pendientes; si el mismo objeto vuelve a cambiar después, se genera
          una alerta nueva. La atribución es siempre manual: el panel muestra, como apoyo, la «evidencia técnica» (si
          el propio agente de Nexus ejecutó el DDL que coincide), pero deja claro que esa evidencia no prueba
          autoría por sí sola.
        </P>
        <H3 id="es-reclasificar">Reclasificar</H3>
        <P>
          Si un cambio ya entendido se atribuyó al responsable equivocado, use <UiLabel>Reclasificar</UiLabel>{" "}
          (exige un motivo). Queda en el historial tanto la atribución original como la nueva.
        </P>
        <H3 id="es-no-verificable">«No se pudo verificar la estructura»</H3>
        <P>
          Si el agente no logró inventariar una base (no hay conexión, credenciales inválidas, el motor no está
          soportado, etc.), el panel muestra <UiLabel>No se pudo verificar la estructura</UiLabel> en vez de comparar
          con datos viejos o inventar una eliminación: se conserva la última comparación confiable hasta que la
          conexión se restablezca.
        </P>
        <Callout tone="perm" title="Permiso necesario">
          Dar por entendido: <UiLabel>Dar por entendido (atribuir)</UiLabel>. Reclasificar: <UiLabel>Reclasificar cambios</UiLabel>.
          Aprobar/reiniciar línea base: <UiLabel>Aprobar línea base</UiLabel>. Configurar bases monitoreadas: <UiLabel>Configurar inventario</UiLabel>.
        </Callout>
      </div>
    ),
  },
  {
    id: "usuarios-auditoria",
    title: "Usuarios y Auditoría",
    icon: UserCog,
    keywords: "usuarios roles permisos alcance auditoría admins sesiones activas desbloquear reinicio de contraseña",
    body: (
      <div className="space-y-4">
        <P>
          <UiLabel>Usuarios</UiLabel> y <UiLabel>Auditoría</UiLabel> son visibles solo para quienes tienen los
          permisos <UiLabel>Administrar usuarios</UiLabel> y <UiLabel>Ver auditoría</UiLabel> respectivamente (ambos
          son de <b>alcance global</b>: no existen por grupo).
        </P>
        <H3 id="ua-usuarios">Usuarios</H3>
        <P>
          Desde <UiLabel>Usuarios</UiLabel> un administrador puede: crear un usuario con su <b>correo</b> (obligatorio
          y único: con él iniciará sesión; se genera una contraseña temporal que el nuevo usuario deberá cambiar en su
          primer inicio), editar sus datos —incluido el correo—, activarlo/desactivarlo, asignarle roles con su
          alcance (todos los grupos o uno en concreto), reiniciar su contraseña, desbloquearlo (botón{" "}
          <UiLabel>Desbloquear</UiLabel>) y cerrar sus <b>sesiones activas</b>. El campo <UiLabel>Usuario</UiLabel> es
          solo un identificador interno/visible: si se deja vacío al crear la cuenta, se deriva automáticamente del
          correo. Un usuario sin correo asignado no puede iniciar sesión. Solo un <b>superadministrador</b> puede
          crear, editar, desactivar, desbloquear, reiniciar la contraseña o cambiar los roles de{" "}
          <b>otro superadministrador</b>; nadie puede quitarse a sí mismo el rol de superadministrador ni sus propios
          roles (debe hacerlo otro administrador), y siempre queda al menos un superadministrador activo.
        </P>
        <H3 id="ua-auditoria">Auditoría</H3>
        <P>
          <UiLabel>Auditoría</UiLabel> registra toda mutación hecha desde el panel (quién, cuándo, qué acción, sobre
          qué recurso, de qué grupo, con qué resultado), incluidas las rechazadas por falta de permiso, además de
          inicios y cierres de sesión, intentos fallidos/bloqueados y cambios de contraseña. Nunca guarda contraseñas
          ni tokens completos. Se filtra por grupo, actor, acción, resultado y fechas, y cada usuario solo ve la
          auditoría de los grupos dentro de su alcance.
        </P>
      </div>
    ),
  },
  {
    id: "faq",
    title: "Preguntas frecuentes",
    icon: HelpCircle,
    keywords:
      "faq solución de problemas agente sin contacto probar conexión sin agente en línea dwh_ssl_error cuenta bloqueada troubleshooting",
    body: (
      <div className="space-y-4">
        <H3 id="faq-sin-contacto">El agente aparece «sin contacto» / desconectado</H3>
        <P>
          Revise que el servicio «Nexus DWH Agent» siga en ejecución en la máquina de la sede (
          <Code>sc query NexusAgent</Code> o el visor de servicios de Windows) y que tenga salida HTTPS hacia la URL
          del backend (el agente solo abre conexiones salientes; no requiere ningún puerto de entrada). Revise el
          registro <Code>logs\nexus_agent.log</Code> junto al programa. Una desconexión tarda entre el umbral
          configurado y ese umbral más el ciclo del evaluador (30 s) en marcarse como incidencia; no es instantáneo.
        </P>
        <H3 id="faq-sin-agente-en-linea">«Probar conexión» dice «sin agente en línea»</H3>
        <P>
          Significa que ningún agente con alcance sobre ese grupo/empresa y en línea (contacto reciente) anunció que
          puede tomar pruebas de conexión, lo que requiere la versión <b>5.3</b> del agente o superior. Verifique en{" "}
          <UiLabel>Instalaciones</UiLabel> que exista al menos una instalación activa y actualizada para ese alcance.
        </P>
        <H3 id="faq-ssl-error">DWH_SSL_ERROR</H3>
        <P>
          El agente no pudo establecer o verificar la conexión cifrada al DWH con el <Code>sslmode</Code> configurado:
          revise que el servidor tenga SSL habilitado si pidió <Code>require</Code> o superior, y que el certificado
          de la CA (PEM) cargado en el grupo/empresa sea el correcto si pidió <Code>verify-ca</Code> o{" "}
          <Code>verify-full</Code>.
        </P>
        <H3 id="faq-cuenta-bloqueada">Mi cuenta está bloqueada</H3>
        <P>
          Tras varios intentos fallidos de contraseña, la cuenta se bloquea temporalmente y el tiempo de espera crece
          con cada intento adicional (aunque escriba la contraseña correcta durante el bloqueo, no entrará). Espere el
          tiempo indicado o pida a un administrador de usuarios que use <UiLabel>Desbloquear</UiLabel>.
        </P>
        <H3 id="faq-tareas-retenidas">Veo «Tareas retenidas» en una instalación</H3>
        <P>
          Esa instalación usa una versión del agente anterior a 5.3 y hay extractores que necesitan funciones que solo
          entiende 5.3 (destino propio por empresa, SSL obligatorio o esquemas con DDL de catálogo). Actualice el
          agente con <Code>update_agent.ps1</Code> (sección «Instalaciones y agente»); las tareas se entregarán solas
          en cuanto la versión sea 5.3 o superior.
        </P>
      </div>
    ),
  },
];
