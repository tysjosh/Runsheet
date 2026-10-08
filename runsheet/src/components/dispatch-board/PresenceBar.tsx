/**
 * Who else has this service day open (R14.4). Names come from the server
 * (`board_presence`, K17.1). Presence never blocks anyone.
 */
import type { BoardPresenceUser } from "../../hooks/useDispatchBoardSocket";
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
          className="flex h-8 w-8 items-center justify-center rounded-full border-2 border-white bg-primary text-xs font-semibold text-white"
        >
          <span aria-hidden="true">{initials(names[i])}</span>
          <span className="sr-only">{names[i]}</span>
        </li>
      ))}
      {users.length > 5 && (
        <li className="flex h-8 w-8 items-center justify-center rounded-full border-2 border-white bg-gray-200 text-xs font-semibold text-gray-700">
          +{users.length - 5}
        </li>
      )}
    </ul>
  );
}

export default PresenceBar;
