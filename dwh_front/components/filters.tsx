"use client";

import type { Agency, Company, Group } from "@/lib/types";
import { Select } from "@/components/ui/primitives";

export interface FilterValue {
  group_id: string;
  company_id: string;
  agency_id?: string;
}

/** Filtros encadenados grupo → empresa (→ agencia). */
export function HierarchyFilters({
  value,
  onChange,
  groups,
  companies,
  agencies,
}: {
  value: FilterValue;
  onChange: (v: FilterValue) => void;
  groups: Group[];
  companies: Company[];
  agencies?: Agency[];
}) {
  const companyOptions = value.group_id ? companies.filter((c) => String(c.group_id) === value.group_id) : companies;
  const agencyOptions = (agencies ?? []).filter(
    (a) => (!value.company_id || String(a.company_id) === value.company_id) && (!value.group_id || String(a.group_id) === value.group_id),
  );
  return (
    <div className="flex flex-wrap gap-2">
      <Select
        aria-label="Filtrar por grupo"
        className="w-full sm:w-44"
        value={value.group_id}
        onChange={(e) => onChange({ group_id: e.target.value, company_id: "", agency_id: agencies ? "" : undefined })}
      >
        <option value="">Todos los grupos</option>
        {groups.map((g) => (
          <option key={g.id} value={g.id}>
            {g.name}
          </option>
        ))}
      </Select>
      <Select
        aria-label="Filtrar por empresa"
        className="w-full sm:w-52"
        value={value.company_id}
        onChange={(e) => onChange({ ...value, company_id: e.target.value, agency_id: agencies ? "" : undefined })}
      >
        <option value="">Todas las empresas</option>
        {companyOptions.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </Select>
      {agencies && (
        <Select aria-label="Filtrar por agencia" className="w-full sm:w-52" value={value.agency_id ?? ""} onChange={(e) => onChange({ ...value, agency_id: e.target.value })}>
          <option value="">Todas las agencias</option>
          {agencyOptions.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </Select>
      )}
    </div>
  );
}

/** <option>s de empresas agrupadas por grupo. */
export function CompanyOptions({ companies }: { companies: Company[] }) {
  const byGroup = new Map<string, Company[]>();
  companies.forEach((c) => byGroup.set(c.group_name, [...(byGroup.get(c.group_name) ?? []), c]));
  return (
    <>
      {Array.from(byGroup.entries()).map(([g, list]) => (
        <optgroup key={g} label={g}>
          {list.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </optgroup>
      ))}
    </>
  );
}
