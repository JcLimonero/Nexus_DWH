"use client";

import type { Company } from "@/lib/types";

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
