import { NavLink, useNavigate } from 'react-router-dom'
import { cn } from '../ui/cn'
import { Logo } from '../ui/Logo'
import { ModeSwitch } from './ModeSwitch'

interface NavItem {
  to: string
  icon: string
  label: string
}

const navItems: NavItem[] = [
  { to: '/', icon: '💬', label: 'Chat' },
  { to: '/settings', icon: '⚙️', label: 'Settings' },
]

export function Sidebar() {
  const navigate = useNavigate()

  return (
    <aside
      className="fixed left-0 top-0 bottom-0 w-16 bg-surface-base border-r border-surface-border flex flex-col z-20"
      aria-label="Main navigation"
    >
      <button
        onClick={() => navigate('/')}
        aria-label="Home"
        className="flex items-center justify-center h-14 shrink-0 hover:opacity-80 transition-opacity"
      >
        <Logo size={28} />
      </button>

      <nav className="flex flex-col items-center gap-4 pt-2 flex-1">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            aria-label={item.label}
            title={item.label}
            className={({ isActive }) =>
              cn(
                'size-10 flex items-center justify-center rounded-md text-lg transition-colors duration-150',
                isActive
                  ? 'bg-brand-primary-dim border border-[var(--accent-border)] text-brand-primary shadow-blue'
                  : 'text-text-secondary hover:bg-surface-overlay hover:text-text-body',
              )
            }
          >
            {item.icon}
          </NavLink>
        ))}
      </nav>

      <div className="flex shrink-0 items-center justify-center pb-4">
        <ModeSwitch direction="vertical" />
      </div>
    </aside>
  )
}
