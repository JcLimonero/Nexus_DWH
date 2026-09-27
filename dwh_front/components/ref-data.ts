"use client";

import { useApi } from "@/lib/api";
import type { Agency, Company, Group, ListResponse } from "@/lib/types";

/** Listas de referencia para selectores (grupos, empresas, agencias). */
export function useRefData(opts: { agencies?: boolean } = {}) {
  const groups = useApi<ListResponse<Group>>("admin/groups");
  const companies = useApi<ListResponse<Company>>("admin/companies");
  const agencies = useApi<ListResponse<Agency>>(opts.agencies ? "admin/agencies" : null);
  return {
    groups: groups.data?.items ?? [],
    companies: companies.data?.items ?? [],
    agencies: agencies.data?.items ?? [],
    reloadRefs: () => {
      void groups.reload();
      void companies.reload();
      if (opts.agencies) void agencies.reload();
    },
  };
}
