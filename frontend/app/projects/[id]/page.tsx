import ProjectClient from "./ProjectClient";

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  return (
    <main style={{ maxWidth: 1400, margin: "0 auto", padding: 16 }}>
      <ProjectClient id={id} />
    </main>
  );
}
