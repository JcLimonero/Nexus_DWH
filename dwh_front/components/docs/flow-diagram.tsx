/**
 * Diagrama de flujo general de Nexus DWH: sede del cliente (agente) ↔ Nexus (panel/backend).
 * SVG en línea, con currentColor para el texto/trazos neutros y colores fijos (teal/violeta)
 * para distinguir "sede del cliente" de "Nexus". Responsive (viewBox + width 100%) y accesible
 * (role="img" + <title>/<desc>).
 */
export function FlowDiagram({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 900 480"
      className={className}
      width="100%"
      role="img"
      aria-labelledby="flow-diagram-title flow-diagram-desc"
    >
      <title id="flow-diagram-title">Flujo general de Nexus DWH</title>
      <desc id="flow-diagram-desc">
        En la sede del cliente, el agente NexusAgent extrae datos del DMS de origen y los carga (upsert) en el DWH
        destino. El agente inicia una conexión HTTPS saliente hacia el backend de Nexus, que expone el panel web a
        través de un proxy /admin y guarda la configuración y los eventos en su base de datos. Nexus nunca abre
        conexiones hacia la red del cliente.
      </desc>

      {/* Contenedor: Sede del cliente */}
      <rect x="24" y="24" width="380" height="400" rx="14" fill="none" stroke="#0d9488" strokeOpacity="0.55" strokeWidth="2" strokeDasharray="7 6" />
      <text x="44" y="54" fontSize="15" fontWeight="700" fill="#0f766e">
        Sede del cliente
      </text>

      {/* Contenedor: Nexus */}
      <rect x="496" y="24" width="380" height="400" rx="14" fill="none" stroke="#7c3aed" strokeOpacity="0.5" strokeWidth="2" strokeDasharray="7 6" />
      <text x="516" y="54" fontSize="15" fontWeight="700" fill="#6d28d9">
        Nexus
      </text>

      {/* ── Nodos: sede del cliente (teal) ── */}
      <Node x={64} y={78} w={300} h={54} fill="#ecfdf5" stroke="#0d9488" lines={["DMS de origen", "(SQL Server, MySQL, Firebird…)"]} />
      <Arrow x1={214} y1={132} x2={214} y2={178} label="Extrae" labelColor="#0f766e" />
      <Node x={64} y={182} w={300} h={54} fill="#ccfbf1" stroke="#0d9488" lines={["NexusAgent", "(Servicio Windows)"]} bold />
      <Arrow x1={214} y1={236} x2={214} y2={282} label="Carga (upsert)" labelColor="#0f766e" />
      <Node x={64} y={286} w={300} h={54} fill="#ecfdf5" stroke="#0d9488" lines={["DWH destino", "(PostgreSQL por grupo o empresa)"]} />

      {/* ── Nodos: Nexus (violeta) ── */}
      <Node x={536} y={78} w={300} h={44} fill="#f5f3ff" stroke="#7c3aed" lines={["Panel web"]} />
      <Arrow x1={686} y1={122} x2={686} y2={158} />
      <Node x={536} y={162} w={300} h={44} fill="#f5f3ff" stroke="#7c3aed" lines={["Proxy /admin"]} />
      <Arrow x1={686} y1={206} x2={686} y2={242} />
      <Node x={536} y={246} w={300} h={54} fill="#ede9fe" stroke="#7c3aed" lines={["Backend FastAPI", "(/agent, /admin, evaluador)"]} bold />
      <Arrow x1={686} y1={300} x2={686} y2={336} label="Config, eventos" labelColor="#6d28d9" />
      <Node x={536} y={340} w={300} h={44} fill="#f5f3ff" stroke="#7c3aed" lines={["BD de configuración"]} />

      {/* Flecha horizontal: agente → backend, "HTTPS saliente" */}
      <line x1="364" y1="209" x2="530" y2="273" stroke="#334155" strokeWidth="2" markerEnd="url(#arrowhead)" />
      <rect x="378" y="216" width="128" height="20" rx="4" fill="#f8fafc" />
      <text x="442" y="230" fontSize="12" fontWeight="600" textAnchor="middle" fill="#334155">
        HTTPS saliente
      </text>

      <defs>
        <marker id="arrowhead" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">
          <path d="M0,0 L9,4.5 L0,9 Z" fill="#334155" />
        </marker>
      </defs>

      {/* Pie de nota */}
      <text x="450" y="458" fontSize="12.5" fontWeight="600" textAnchor="middle" fill="#475569">
        El agente inicia todas las conexiones; Nexus nunca entra a la red del cliente.
      </text>
    </svg>
  );
}

function Node({
  x,
  y,
  w,
  h,
  lines,
  fill,
  stroke,
  bold,
}: {
  x: number;
  y: number;
  w: number;
  h: number;
  lines: string[];
  fill: string;
  stroke: string;
  bold?: boolean;
}) {
  const cx = x + w / 2;
  const cy = y + h / 2;
  const lineHeight = 17;
  const start = cy - ((lines.length - 1) * lineHeight) / 2;
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={10} fill={fill} stroke={stroke} strokeWidth="1.5" />
      {lines.map((line, i) => (
        <text
          key={line}
          x={cx}
          y={start + i * lineHeight + 5}
          textAnchor="middle"
          fontSize={i === 0 ? 14 : 12}
          fontWeight={i === 0 && bold ? 700 : i === 0 ? 600 : 400}
          fill="#1e293b"
        >
          {line}
        </text>
      ))}
    </g>
  );
}

function Arrow({
  x1,
  y1,
  x2,
  y2,
  label,
  labelColor,
}: {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  label?: string;
  labelColor?: string;
}) {
  return (
    <g>
      <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#64748b" strokeWidth="2" markerEnd="url(#arrowhead)" />
      {label && (
        <text x={x1 + (x2 - x1) / 2 + 60} y={y1 + (y2 - y1) / 2 + 4} fontSize="11.5" fontWeight="600" textAnchor="middle" fill={labelColor ?? "#334155"}>
          {label}
        </text>
      )}
    </g>
  );
}
