/** What a move to Automatic starts on its own, listed in its confirm dialog (mockup F). */
export function JobList({ items }: { items: string[] }) {
  return (
    <ul
      aria-label="Starts on its own"
      className="list-disc rounded-lg bg-ok-soft py-3 pr-3.5 pl-8 text-foreground ring-1 ring-ok/35"
    >
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  )
}
