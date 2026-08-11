"use client";

import { Canvas, useFrame } from "@react-three/fiber";
import { OrbitControls, Float, Text } from "@react-three/drei";
import { useRef } from "react";

function HeroObject() {
  const mesh = useRef();
  useFrame((_, delta) => {
    mesh.current.rotation.x += delta * 0.22;
    mesh.current.rotation.y += delta * 0.35;
  });

  return (
    <Float speed={1.5} rotationIntensity={0.3} floatIntensity={0.6}>
      <mesh ref={mesh} castShadow>
        <icosahedronGeometry args={[1.25, 2]} />
        <meshStandardMaterial color="#7c5cff" metalness={0.55} roughness={0.2} />
      </mesh>
    </Float>
  );
}

export default function Home() {
  return (
    <main>
      <section className="hero">
        <div className="copy">
          <p className="eyebrow">CHITTI / MOTION LAB</p>
          <h1>Ideas with a little more dimension.</h1>
          <p className="lede">
            A deterministic Next.js landing page fixture with a live React Three
            Fiber scene, ready for a safe build.
          </p>
          <button type="button">Explore the scene</button>
        </div>
        <div className="scene" aria-label="Animated 3D preview">
          <Canvas camera={{ position: [0, 0, 4.5], fov: 45 }} shadows>
            <ambientLight intensity={1.2} />
            <directionalLight position={[3, 4, 5]} intensity={2.5} castShadow />
            <HeroObject />
            <Text position={[0, -2, 0]} fontSize={0.22} color="#d7d2ff">
              BUILD / SEE / REFINE
            </Text>
            <OrbitControls enableZoom={false} autoRotate autoRotateSpeed={0.4} />
          </Canvas>
        </div>
      </section>
    </main>
  );
}
