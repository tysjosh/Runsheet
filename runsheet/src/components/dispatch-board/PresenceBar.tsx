/**
 * Who else has this service day open (R14.4). Names come from the server
 * (`board_presence`, K17.1). Presence never blocks anyone.
 */
import type { BoardPresenceUser } from "../../hooks/useDispatchBoardSocket";
import { identityFor } from "../../lib/identity";
import { actorDisplayName } from "./state/announce";

function initials(name: string): string {
  const parts = name.split(/[\s._-]+/).filter(Boolean);
  return (
    (parts[0]?.[0] ?? "?").toUpperCase() + (parts[1]?.[0] ?? "").toUpperCase()
  );
}

export interface PresenceBarProps {
  users: BoardPresenceUser[];
}

export function PresenceBar({ users }: PresenceBarProps) {
  if (users.length === 0) return null;
  const names = users.map((u) => actorDisplayName(u.name));
  return (
    <ul
      aria-label={`Also viewing: ${names.join(", ")}`}
      className="flex -space-x-2"
    >
      {users.slice(0, 5).map((u, i) => (
        <li
          key={u.user_id}
          title={names[i]}
          className="flex h-6 w-6 items-center justify-center rounded-full border-2 border-white text-[10px] font-bold text-white"
          style={{ backgroundColor: identityFor(u.user_id).hex }}
        >
          <span aria-hidden="true">{initials(names[i])}</span>
          <span className="sr-only">{names[i]}</span>
        </li>
      ))}
      {users.length > 5 && (
        <li className="flex h-6 w-6 items-center justify-center rounded-full border-2 border-white bg-slate-200 text-[10px] font-semibold text-slate-800">
          +{users.length - 5}
        </li>
      )}
    </ul>
  );
}

export default PresenceBar;
