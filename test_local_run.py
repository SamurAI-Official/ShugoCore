#!/usr/bin/env python3
"""
Local test script for ShugoCore on MacBook Air M4.
Tests simulation framework and AI harness with local Ollama.
"""

import sys
import time

def test_mujoco():
    """Test MuJoCo physics engine."""
    print("\n=== Testing MuJoCo Physics ===")
    try:
        import mujoco
        import numpy as np
        
        print(f"MuJoCo version: {mujoco.__version__}")
        
        # Create simple scene
        xml = '''
        <mujoco>
            <worldbody>
                <geom name="floor" type="plane" size="10 10 0.1"/>
                <body name="box" pos="0 0 1.0">
                    <joint type="free"/>
                    <geom name="box_geom" type="box" size="0.1 0.1 0.1" mass="1"/>
                </body>
            </worldbody>
        </mujoco>
        '''
        
        m = mujoco.MjModel.from_xml_string(xml)
        d = mujoco.MjData(m)
        
        # Simulate 100 steps
        for _ in range(100):
            mujoco.mj_step(m, d)
        
        print(f"Physics step: OK")
        print(f"Box final height: {d.qpos[2]:.3f}m (should be ~0.9m due to gravity)")
        return True
    except Exception as e:
        print(f"MuJoCo test FAILED: {e}")
        return False


def test_simulation_framework():
    """Test the simulation framework."""
    print("\n=== Testing Simulation Framework ===")
    try:
        from simulation import create_simulation
        from simulation.robots import list_robots, get_robot_model, BerkeleyHumanoidLite
        
        print(f"Available robots: {list_robots()}")
        
        # Test Berkeley Humanoid Lite
        robot = get_robot_model("berkeley_humanoid_lite")
        print(f"Robot: {robot.name}")
        print(f"Joints: {robot.num_joints}")
        print(f"Joint names (first 5): {robot.get_joint_names()[:5]}")
        
        # Create simulation
        sim = create_simulation(robot_name="berkeley_humanoid_lite")
        print(f"Simulation type: {type(sim).__name__}")
        print(f"Simulation available: {sim.is_available()}")
        
        return True
    except Exception as e:
        print(f"Simulation framework test FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_ollama_connection():
    """Test connection to local Ollama server."""
    print("\n=== Testing Ollama Connection ===")
    try:
        from model_backends import OllamaBackend
        
        backend = OllamaBackend(base_url="http://127.0.0.1:11434")
        
        # List available models
        models = backend.list_models()
        print(f"Available models: {models}")
        
        if "qwen3.5:latest" in models:
            print("qwen3.5 model found!")
            return True
        else:
            print("qwen3.5 model NOT found")
            return False
            
    except Exception as e:
        print(f"Ollama connection test FAILED: {e}")
        return False


def test_ollama_generation():
    """Test Ollama text generation."""
    print("\n=== Testing Ollama Generation ===")
    try:
        from model_backends import OllamaBackend
        
                # 300s tolerates the first-load cold-start of the 6.5GB qwen3.5 model (can exceed 120s).
        backend = OllamaBackend(base_url="http://127.0.0.1:11434", timeout=300.0)

        start = time.time()
        response = backend.generate(
            "qwen3.5:latest",
                        "Say 'hello' in one word.",
            timeout=300.0
        )
        elapsed = time.time() - start
        
        print(f"Response: {response}")
        print(f"Time: {elapsed:.2f}s")
        return len(response) > 0
        
    except Exception as e:
        print(f"Ollama generation test FAILED: {e}")
        return False


def test_decision_engine():
    """Test ShugoCore decision engine with Ollama."""
    print("\n=== Testing Decision Engine ===")
    try:
        from decision_engine import DecisionEngine
        from model_backends import OllamaBackend
        
        backend = OllamaBackend(base_url="http://127.0.0.1:11434", timeout=300.0)
        
        # Create engine with Ollama backend
        # Use chroma for vector DB (will fall back to stub if not installed)
        engine = DecisionEngine(
            models=[{
                "id": "qwen3.5:latest",
                "backend": {"type": "ollama"}
            }],
            vector_db_config={"type": "chroma", "collection_name": "test"},
            memory_db_path=":memory:"
        )
        
        print("DecisionEngine initialized with Ollama backend")
        return True
        
    except Exception as e:
        print(f"Decision engine test FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("=" * 50)
    print("ShugoCore Local Test - MacBook Air M4")
    print("=" * 50)
    
    results = {}
    
    # Test 1: MuJoCo Physics
    results["MuJoCo Physics"] = test_mujoco()
    
    # Test 2: Simulation Framework
    results["Simulation Framework"] = test_simulation_framework()
    
    # Test 3: Ollama Connection
    results["Ollama Connection"] = test_ollama_connection()
    
    # Test 4: Ollama Generation
    results["Ollama Generation"] = test_ollama_generation()
    
    # Test 5: Decision Engine
    results["Decision Engine"] = test_decision_engine()
    
    # Summary
    print("\n" + "=" * 50)
    print("Test Summary")
    print("=" * 50)
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
    
    all_passed = all(results.values())
    print("\n" + ("All tests PASSED!" if all_passed else "Some tests FAILED"))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
