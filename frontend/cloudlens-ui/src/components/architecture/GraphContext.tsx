import { createContext, useContext, useEffect, useState } from "react";

import { loadIconManifest, type IconManifest } from "../../lib/icons";

export interface GraphActions {
  /** Open the detail panel for a node id (resource, external, or a group's node_id). */
  openNode: (nodeId: string) => void;
  selectedNodeId: string | null;
  iconManifest: IconManifest;
}

export const GraphActionsContext = createContext<GraphActions>({
  openNode: () => undefined,
  selectedNodeId: null,
  iconManifest: {},
});

export function useGraphActions(): GraphActions {
  return useContext(GraphActionsContext);
}

export function useIconManifest(): IconManifest {
  const [manifest, setManifest] = useState<IconManifest>({});
  useEffect(() => {
    let active = true;
    loadIconManifest().then((m) => {
      if (active) setManifest(m);
    });
    return () => {
      active = false;
    };
  }, []);
  return manifest;
}
